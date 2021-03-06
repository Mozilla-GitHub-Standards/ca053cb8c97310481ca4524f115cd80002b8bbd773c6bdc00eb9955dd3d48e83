# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import cgi
from collections import deque
import hashlib
import io
import logging
import json
import time
import zlib

from everett.component import ConfigOptions, RequiredConfigMixin
from everett.manager import parse_class
import falcon
from falcon.request_helpers import BoundedStream
from gevent.pool import Pool
import markus

from antenna.heartbeat import register_for_life, register_for_heartbeat
from antenna.throttler import (
    REJECT,
    FAKEACCEPT,
    RESULT_TO_TEXT,
    Throttler,
)
from antenna.util import (
    create_crash_id,
    sanitize_dump_name,
    utc_now,
    validate_crash_id,
)


logger = logging.getLogger(__name__)
mymetrics = markus.get_metrics('breakpad_resource')


#: Maximum number of attempts to save a crash before we give up
MAX_ATTEMPTS = 20

#: SAVE and PUBLISH states of the crash mover
STATE_SAVE = 'save'
STATE_PUBLISH = 'publish'


class CrashReport:
    """Crash report structure."""

    def __init__(self, raw_crash, dumps, crash_id, errors=0):
        self.raw_crash = raw_crash
        self.dumps = dumps
        self.crash_id = crash_id
        self.errors = errors

        self.state = None

    def set_state(self, state):
        """Set new state and reset errors."""
        self.state = state
        self.errors = 0


def positive_int(val):
    """Everett parser that enforces val >= 1."""
    val = int(val)
    if val < 1:
        raise ValueError('val must be greater than 1: %s' % val)
    return val


class BreakpadSubmitterResource(RequiredConfigMixin):
    """Handles incoming breakpad crash reports and saves to crashstorage.

    This handles incoming HTTP POST requests containing breakpad-style crash
    reports in multipart/form-data format.

    It can handle compressed or uncompressed POST payloads.

    It parses the payload from the HTTP POST request, runs it through the
    throttler with the specified rules, generates a crash_id, returns the
    crash_id to the HTTP client and then saves the crash using the configured
    crashstorage class.

    .. Note::

       From when a crash comes in to when it's saved by the crashstorage class,
       the crash is entirely in memory. Keep that in mind when figuring out
       how to scale your Antenna nodes.


    The most important configuration bit here is choosing the crashstorage
    class.

    For example::

        CRASHSTORAGE_CLASS=antenna.ext.s3.crashstorage.S3CrashStorage

    """

    required_config = ConfigOptions()
    required_config.add_option(
        'dump_field', default='upload_file_minidump',
        doc='The name of the field in the POST data for dumps.'
    )
    required_config.add_option(
        'dump_id_prefix', default='bp-',
        doc='The crash type prefix.'
    )
    required_config.add_option(
        'concurrent_crashmovers',
        default='2',
        parser=positive_int,
        doc=(
            'The number of crashes concurrently being saved and published. '
            'Each process gets this many concurrent crashmovers, so if you\'re '
            'running 5 processes on the node, then it\'s '
            '(5 * concurrent_crashmovers) sharing upload bandwidth.'
        )
    )

    # crashstorage things
    required_config.add_option(
        'crashstorage_class',
        default='antenna.ext.crashstorage_base.NoOpCrashStorage',
        parser=parse_class,
        doc='The class in charge of storing crashes.'
    )

    # crashpublish things
    required_config.add_option(
        'crashpublish_class',
        default='antenna.ext.crashpublish_base.NoOpCrashPublish',
        parser=parse_class,
        doc='The class in charge of publishing crashes.'
    )

    def __init__(self, config):
        self.config = config.with_options(self)
        self.crashstorage = self.config('crashstorage_class')(config.with_namespace('crashstorage'))
        self.crashpublish = self.config('crashpublish_class')(config.with_namespace('crashpublish'))
        self.throttler = Throttler(config)

        # Gevent pool for crashmover workers
        self.crashmover_pool = Pool(size=self.config('concurrent_crashmovers'))

        # Queue for crashmover work
        self.crashmover_queue = deque()

        # Register hb functions with heartbeat manager
        register_for_heartbeat(self.hb_report_health_stats)
        register_for_heartbeat(self.hb_run_crashmover)

        # Register life function with heartbeat manager
        register_for_life(self.has_work_to_do)

    def get_runtime_config(self, namespace=None):
        """Return generator of runtime configuration."""
        for item in super().get_runtime_config():
            yield item

        for item in self.throttler.get_runtime_config():
            yield item

        for item in self.crashstorage.get_runtime_config(['crashstorage']):
            yield item

        for item in self.crashpublish.get_runtime_config(['crashpublish']):
            yield item

    def check_health(self, state):
        """Return health state."""
        if hasattr(self.crashstorage, 'check_health'):
            self.crashstorage.check_health(state)
        if hasattr(self.crashpublish, 'check_health'):
            self.crashpublish.check_health(state)

    def hb_report_health_stats(self):
        """Heartbeat function to report health stats."""
        # The number of crash reports sitting in the work queue; this is a
        # direct measure of the health of this process--a number that's going
        # up means impending doom
        mymetrics.gauge('work_queue_size', value=len(self.crashmover_queue))

    def has_work_to_do(self):
        """Return whether this still has work to do."""
        work_to_do = (
            len(self.crashmover_pool) +
            len(self.crashmover_queue)
        )
        logger.info('work left to do: %s' % work_to_do)
        # Indicates whether or not we're sitting on crashes to save--this helps
        # keep Antenna alive until we're done saving crashes
        return bool(work_to_do)

    def extract_payload(self, req):
        """Parse HTTP POST payload.

        Decompresses the payload if necessary and then walks through the
        FieldStorage converting from multipart/form-data to Python datatypes.

        NOTE(willkg): The FieldStorage is poorly documented (in my opinion). It
        has a list attribute that is a list of FieldStorage items--one for each
        key/val in the form. For attached files, the FieldStorage will have a
        name, value and filename and the type should be
        application/octet-stream. Thus we parse it looking for things of type
        text/plain and application/octet-stream.

        :arg falcon.request.Request req: a Falcon Request instance

        :returns: (raw_crash dict, dumps dict)

        """
        # If we don't have a content type, return an empty crash
        if not req.content_type:
            mymetrics.incr('malformed', tags=['reason:no_content_type'])
            return {}, {}

        # If it's the wrong content type or there's no boundary section, return
        # an empty crash
        content_type = [part.strip() for part in req.content_type.split(';', 1)]
        if ((len(content_type) != 2 or
             content_type[0] != 'multipart/form-data' or
             not content_type[1].startswith('boundary='))):
            if content_type[0] != 'multipart/form-data':
                mymetrics.incr('malformed', tags=['reason:wrong_content_type'])
            else:
                mymetrics.incr('malformed', tags=['reason:no_boundary'])
            return {}, {}

        content_length = req.content_length or 0

        # If there's no content, return an empty crash
        if content_length == 0:
            mymetrics.incr('malformed', tags=['reason:no_content_length'])
            return {}, {}

        # Decompress payload if it's compressed
        if req.env.get('HTTP_CONTENT_ENCODING') == 'gzip':
            mymetrics.incr('gzipped_crash')

            # If the content is gzipped, we pull it out and decompress it. We
            # have to do that here because nginx doesn't have a good way to do
            # that in nginx-land.
            gzip_header = 16 + zlib.MAX_WBITS
            try:
                data = zlib.decompress(req.stream.read(content_length), gzip_header)
            except zlib.error:
                # This indicates this isn't a valid compressed stream. Given
                # that the HTTP request insists it is, we're just going to
                # assume it's junk and not try to process any further.
                mymetrics.incr('malformed', tags=['reason:bad_gzip'])
                return {}, {}

            # Stomp on the content length to correct it because we've changed
            # the payload size by decompressing it. We save the original value
            # in case we need to debug something later on.
            req.env['ORIG_CONTENT_LENGTH'] = content_length
            content_length = len(data)
            req.env['CONTENT_LENGTH'] = str(content_length)

            data = io.BytesIO(data)
            mymetrics.histogram('crash_size', value=content_length, tags=['payload:compressed'])
        else:
            # NOTE(willkg): At this point, req.stream is either a
            # falcon.request_helper.BoundedStream (in tests) or a
            # gunicorn.http.body.Body (in production).
            #
            # FieldStorage doesn't work with BoundedStream so we pluck out the
            # internal stream from that which works fine.
            #
            # FIXME(willkg): why don't tests work with BoundedStream?
            if isinstance(req.stream, BoundedStream):
                data = req.stream.stream
            else:
                data = req.stream

            mymetrics.histogram('crash_size', value=content_length, tags=['payload:uncompressed'])

        fs = cgi.FieldStorage(fp=data, environ=req.env, keep_blank_values=1)

        # NOTE(willkg): In the original collector, this returned request
        # querystring data as well as request body data, but we're not doing
        # that because the query string just duplicates data in the payload.

        raw_crash = {}
        dumps = {}

        has_json = False
        has_kvpairs = False

        for fs_item in fs.list:
            # NOTE(willkg): We saw some crashes come in where the raw crash ends up with
            # a None as a key. Make sure we can't end up with non-strings as keys.
            item_name = fs_item.name or ''

            if item_name == 'dump_checksums':
                # We don't want to pick up the dump_checksums from a raw
                # crash that was re-submitted.
                continue

            elif fs_item.type and fs_item.type.startswith('application/json'):
                # This is a JSON blob, so load it and override raw_crash with
                # it.
                has_json = True
                raw_crash = json.loads(fs_item.value)

            elif fs_item.type and (fs_item.type.startswith('application/octet-stream') or isinstance(fs_item.value, bytes)):
                # This is a dump, so add it to dumps using a sanitized dump
                # name.
                dump_name = sanitize_dump_name(item_name)
                dumps[dump_name] = fs_item.value

            else:
                # This isn't a dump, so it's a key/val pair, so we add that.
                has_kvpairs = True
                raw_crash[item_name] = fs_item.value

        if has_json and has_kvpairs:
            # If the crash payload has both kvpairs and a JSON blob, then it's
            # malformed and we should dump it.
            mymetrics.incr('malformed', tags=['reason:has_json_and_kv'])
            return {}, {}

        return raw_crash, dumps

    def get_throttle_result(self, raw_crash):
        """Run raw_crash through throttler for a throttling result.

        :arg dict raw_crash: the raw crash to throttle

        :returns tuple: ``(result, rule_name, percentage)``

        """
        # At this stage, nothing has given us a throttle answer, so we
        # throttle the crash.
        result, rule_name, throttle_rate = self.throttler.throttle(raw_crash)

        # Save the results in the raw_crash itself
        raw_crash['legacy_processing'] = result
        raw_crash['throttle_rate'] = throttle_rate

        return result, rule_name, throttle_rate

    @mymetrics.timer_decorator('on_post.time')
    def on_post(self, req, resp):
        """Handle incoming HTTP POSTs.

        Note: This is executed by the WSGI app, so it and anything it does is
        covered by the Sentry middleware.

        """
        resp.status = falcon.HTTP_200

        start_time = time.time()
        # NOTE(willkg): This has to return text/plain since that's what the
        # breakpad clients expect.
        resp.content_type = 'text/plain'

        raw_crash, dumps = self.extract_payload(req)

        # If we didn't get any crash data, then just drop it and move on--don't
        # count this as an incoming crash and don't do any more work on it
        if not raw_crash:
            resp.body = 'Discarded=1'
            return

        mymetrics.incr('incoming_crash')

        # Add timestamps
        current_timestamp = utc_now()
        raw_crash['submitted_timestamp'] = current_timestamp.isoformat()
        raw_crash['timestamp'] = start_time

        # Add checksums and MinidumpSha256Hash
        raw_crash['dump_checksums'] = {
            dump_name: hashlib.sha256(dump).hexdigest()
            for dump_name, dump in dumps.items()
        }
        raw_crash['MinidumpSha256Hash'] = raw_crash['dump_checksums'].get('upload_file_minidump', '')

        # First throttle the crash which gives us the information we need
        # to generate a crash id.
        throttle_result, rule_name, percentage = self.get_throttle_result(raw_crash)

        # Use a uuid if they gave us one and it's valid--otherwise create a new
        # one.
        if 'uuid' in raw_crash and validate_crash_id(raw_crash['uuid']):
            crash_id = raw_crash['uuid']
            logger.info('%s has existing crash_id', crash_id)

        else:
            crash_id = create_crash_id(
                timestamp=current_timestamp,
                throttle_result=throttle_result
            )
            raw_crash['uuid'] = crash_id

        raw_crash['type_tag'] = self.config('dump_id_prefix').strip('-')

        # Log the throttle result
        logger.info('%s: matched by %s; returned %s', crash_id, rule_name,
                    RESULT_TO_TEXT[throttle_result])
        mymetrics.incr('throttle_rule', tags=['rule:%s' % rule_name])
        mymetrics.incr('throttle', tags=['result:%s' % RESULT_TO_TEXT[throttle_result].lower()])

        if throttle_result is REJECT:
            # If the result is REJECT, then discard it
            resp.body = 'Discarded=1'

        elif throttle_result is FAKEACCEPT:
            # If the result is a FAKEACCEPT, then we return a crash id, but throw
            # the crash away
            resp.body = 'CrashID=%s%s\n' % (self.config('dump_id_prefix'), crash_id)

        else:
            # If the result is not REJECT, then save it and return the CrashID to
            # the client
            crash_report = CrashReport(raw_crash, dumps, crash_id)
            crash_report.set_state(STATE_SAVE)
            self.crashmover_queue.append(crash_report)
            self.hb_run_crashmover()
            resp.body = 'CrashID=%s%s\n' % (self.config('dump_id_prefix'), crash_id)

    def hb_run_crashmover(self):
        """Spawn a crashmover if there's work to do."""
        # Spawn a new crashmover if there's stuff in the queue and we haven't
        # hit the limit of how many we can run
        if self.crashmover_queue and self.crashmover_pool.free_count() > 0:
            self.crashmover_pool.spawn(self.crashmover_process_queue)

    def crashmover_process_queue(self):
        """Process crashmover work.

        NOTE(willkg): This has to be super careful not to lose crash reports.
        If there's any kind of problem, this must return the crash report to
        the relevant queue.

        """
        while self.crashmover_queue:
            crash_report = self.crashmover_queue.popleft()

            try:
                if crash_report.state == STATE_SAVE:
                    # Save crash and then toss crash_id in the publish queue
                    self.crashmover_save(crash_report)
                    crash_report.set_state(STATE_PUBLISH)
                    self.crashmover_queue.append(crash_report)

                elif crash_report.state == STATE_PUBLISH:
                    # Publish crash and we're done
                    self.crashmover_publish(crash_report)
                    self.crashmover_finish(crash_report)

            except Exception:
                mymetrics.incr('%s_crash_exception.count' % crash_report.state)
                crash_report.errors += 1
                logger.exception(
                    'Exception when processing queue (%s), state: %s; error %d/%d',
                    crash_report.crash_id,
                    crash_report.state,
                    crash_report.errors,
                    MAX_ATTEMPTS
                )

                # After MAX_ATTEMPTS, we give up on this crash and move on
                if crash_report.errors < MAX_ATTEMPTS:
                    self.crashmover_queue.append(crash_report)
                else:
                    logger.error(
                        '%s: too many errors trying to %s; dropped',
                        crash_report.crash_id,
                        crash_report.state
                    )
                    mymetrics.incr('%s_crash_dropped.count' % crash_report.state)

    def crashmover_finish(self, crash_report):
        """Finish bookkeeping on crash report."""
        # Capture the total time it took for this crash to be handled from
        # being received from breakpad client to saving to s3.
        #
        # NOTE(willkg): time.time returns seconds, but .timing() wants
        # milliseconds, so we multiply!
        delta = (time.time() - crash_report.raw_crash['timestamp']) * 1000

        mymetrics.timing('crash_handling.time', value=delta)
        mymetrics.incr('save_crash.count')

    @mymetrics.timer('crash_save.time')
    def crashmover_save(self, crash_report):
        """Save crash report to storage."""
        self.crashstorage.save_crash(crash_report)
        logger.info('%s saved', crash_report.crash_id)

    @mymetrics.timer('crash_publish.time')
    def crashmover_publish(self, crash_report):
        """Publish crash_id in publish queue."""
        self.crashpublish.publish_crash(crash_report)
        logger.info('%s published', crash_report.crash_id)

    def join_pool(self):
        """Join the pool.

        NOTE(willkg): Only use this in tests!

        This is helpful for forcing all the coroutines in the pool to complete
        so that we can verify outcomes in the test suite for work that might
        cross coroutines.

        """
        self.crashmover_pool.join()
