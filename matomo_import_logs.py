#!/usr/bin/env python3 
# vim: et sw=4 ts=4:
# -*- coding: utf-8 -*-
#
# Matomo - free/libre analytics platform
#
# @link https://matomo.org
# @license https://www.gnu.org/licenses/gpl-3.0.html GPL v3 or later
# @version $Id$
#
# For more info see: https://matomo.org/log-analytics/ and https://matomo.org/docs/log-analytics-tool-how-to/
#
# Requires Python >= 3.4
#

import sys



import bz2
import datetime
import gzip
import http.client
import inspect
import itertools
import logging
import os
import os.path
import queue
import re
import sys
import threading
import time
import urllib.request, urllib.parse, urllib.error
import traceback
import socket
import textwrap
import yaml
from argparse import ArgumentParser
from types import MethodType
from time import sleep

try:
    import json
except ImportError:
    try:
        import simplejson as json
    except ImportError:
        print('simplejson (http://pypi.python.org/pypi/simplejson/) is required.', file=sys.stderr)
        sys.exit(1)



##
## Constants.
##

MATOMO_DEFAULT_MAX_ATTEMPTS = 3
MATOMO_DEFAULT_DELAY_AFTER_FAILURE = 10
DEFAULT_SOCKET_TIMEOUT = 300

ARGS = ArgumentParser()
ARGS.add_argument("--debug", action="store_true", help="Enable debugging")
ARGS.add_argument("--server", help="Overwrite the destination matomo server")
ARGS.add_argument("--dry-run", action="store_true", help="Enable dry run, i.e. don't send data to server")
ARGS.add_argument("-s", "--skip", type=int, default=0, help="Skip the given amount of lines in the logs (useful to continue processing)")
ARGS.add_argument("logs", help="Directory containing the log-files")
##
## Formats.
##

class BaseFormatException(Exception): pass

class BaseFormat(object):
    BASE_DATE_FORMAT = '%d/%b/%Y:%H:%M:%S'
    def __init__(self, name):
        self.name = name
        self.regex = None
        self.date_format = BaseFormat.BASE_DATE_FORMAT
        self.parseTime = MethodType(BaseFormat._parseTimeFast, self)

    def check_format(self, file):
        line = file.readline()
        try:
            file.seek(0)
        except IOError:
            pass

        return self.check_format_line(line)

    '''
    This is a shorthand parser if the date is given in the default BASE_DATE_FORMAT
    
    When processing large chunks this actually results in a huge performance improvement
    '''
    def _parseTimeFast(self, date_string):
        year = int(date_string[7:11])
        month = MONTHS[date_string[3:6]]
        day = int(date_string[0:2])
        hour = int(date_string[12:14])
        minute = int(date_string[15:17])
        second = int(date_string[18:20])
        return datetime.datetime(year, month, day, hour, minute, second)        

    def _parseTimeSlow(self, date_string):
        return datetime.datetime.strptime(date_string, self.date_format)

    def check_format_line(self, line):
        return False

class JsonFormat(BaseFormat):
    def __init__(self, name):
        super(JsonFormat, self).__init__(name)
        self.json = None
        self.date_format = '%Y-%m-%dT%H:%M:%S'
        self.parseTime = MethodType(BaseFormat._parseTimeSlow, self)

    def check_format_line(self, line):
        try:
            self.json = json.loads(line)
            return True
        except:
            return False

    def match(self, line):
        try:
            # nginx outputs malformed JSON w/ hex escapes when confronted w/ non-UTF input. we have to
            # workaround this by converting hex escapes in strings to unicode escapes. the conversion is naive,
            # so it does not take into account the string's actual encoding (which we don't have access to).
            line = line.replace('\\x', '\\u00')

            self.json = json.loads(line)
            return self
        except:
            self.json = None
            return None

    def get(self, key):
        # Some ugly patchs ...
        if key == 'generation_time_milli':
            self.json[key] =  int(float(self.json[key]) * 1000)
        # Patch date format ISO 8601
        elif key == 'date':
            tz = self.json[key][19:]
            self.json['timezone'] = tz.replace(':', '')
            self.json[key] = self.json[key][:19]

        try:
            return self.json[key]
        except KeyError:
            raise BaseFormatException()

    def get_all(self,):
        return self.json

    def remove_ignored_groups(self, groups):
        for group in groups:
            del self.json[group]

class RegexFormat(BaseFormat):

    def __init__(self, name, regex, date_format=None):
        super(RegexFormat, self).__init__(name)
        if regex is not None:
            self.regex = re.compile(regex)
        if date_format is not None:
            self.date_format = date_format
            if self.date_format != BaseFormat.BASE_DATE_FORMAT:
                self.parseTime = MethodType(BaseFormat._parseTimeSlow, self)
        self.matched = None

    def check_format_line(self, line):
        return self.match(line)

    def match(self,line):
        if not self.regex:
            return None
        match_result = self.regex.match(line)
        if match_result:
            self.matched = match_result.groupdict()
        else:
            self.matched = None
        return match_result

    def get(self, key):
        try:
            return self.matched[key]
        except KeyError:
            raise BaseFormatException("Cannot find group '%s'." % key)

    def get_all(self,):
        return self.matched

    def remove_ignored_groups(self, groups):
        for group in groups:
            del self.matched[group]

class W3cExtendedFormat(RegexFormat):

    FIELDS_LINE_PREFIX = '#Fields: '

    fields = {
        'date': '(?P<date>\d+[-\d+]+',
        'time': '[\d+:]+)[.\d]*?', # TODO should not assume date & time will be together not sure how to fix ATM.
        'cs-uri-stem': '(?P<path>/\S*)',
        'cs-uri-query': '(?P<query_string>\S*)',
        'c-ip': '"?(?P<ip>[\w*.:-]*)"?',
        'cs(User-Agent)': '(?P<user_agent>".*?"|\S*)',
        'cs(Referer)': '(?P<referrer>\S+)',
        'sc-status': '(?P<status>\d+)',
        'sc-bytes': '(?P<length>\S+)',
        'cs-host': '(?P<host>\S+)',
        'cs-method': '(?P<method>\S+)',
        'cs-username': '(?P<userid>\S+)',
        'time-taken': '(?P<generation_time_secs>[.\d]+)'
    }

    def __init__(self):
        super(W3cExtendedFormat, self).__init__('w3c_extended', None, '%Y-%m-%d %H:%M:%S')

    def check_format(self, file):
        self.create_regex(file)

        # if we couldn't create a regex, this file does not follow the W3C extended log file format
        if not self.regex:
            try:
                file.seek(0)
            except IOError:
                pass

            return

        first_line = file.readline()

        try:
            file.seek(0)
        except IOError:
            pass

        return self.check_format_line(first_line)

    def create_regex(self, file):
        fields_line = None

        # collect all header lines up until the Fields: line
        # if we're reading from stdin, we can't seek, so don't read any more than the Fields line
        header_lines = []
        while fields_line is None:
            line = file.readline().strip()

            if not line:
                continue

            if not line.startswith('#'):
                break

            if line.startswith(W3cExtendedFormat.FIELDS_LINE_PREFIX):
                fields_line = line
            else:
                header_lines.append(line)

        if not fields_line:
            return

        # store the header lines for a later check for IIS
        self.header_lines = header_lines

        # Parse the 'Fields: ' line to create the regex to use
        full_regex = []

        expected_fields = type(self).fields.copy() # turn custom field mapping into field => regex mapping

        # if the --w3c-time-taken-millisecs option is used, make sure the time-taken field is interpreted as milliseconds

        for mapped_field_name, field_name in config.options["custom_w3c_fields"].items():
            expected_fields[mapped_field_name] = expected_fields[field_name]
            del expected_fields[field_name]

        # add custom field regexes supplied through --w3c-field-regex option

        # Skip the 'Fields: ' prefix.
        fields_line = fields_line[9:].strip()
        for field in re.split('\s+', fields_line):
            try:
                regex = expected_fields[field]
            except KeyError:
                regex = '(?:".*?"|\S+)'
            full_regex.append(regex)
        full_regex = '\s+'.join(full_regex)

        logging.debug("Based on 'Fields:' line, computed regex to be %s", full_regex)

        self.regex = re.compile(full_regex)

    def check_for_iis_option(self):
       logging.info("WARNING: IIS log file being parsed without --w3c-time-taken-milli option. IIS"
                         " stores millisecond values in the time-taken field. If your logfile does this, the aforementioned"
                         " option must be used in order to get accurate generation times.")

    def _is_iis(self):
        return len([line for line in self.header_lines if 'internet information services' in line.lower() or 'iis' in line.lower()]) > 0

    def _is_time_taken_milli(self):
        return 'generation_time_milli' not in self.regex.pattern

class IisFormat(W3cExtendedFormat):

    fields = W3cExtendedFormat.fields.copy()
    fields.update({
        'time-taken': '(?P<generation_time_milli>[.\d]+)',
        'sc-win32-status': '(?P<__win32_status>\S+)' # this group is useless for log importing, but capturing it
                                                     # will ensure we always select IIS for the format instead of
                                                     # W3C logs when detecting the format. This way there will be
                                                     # less accidental importing of IIS logs w/o --w3c-time-taken-milli.
    })

    def __init__(self):
        super(IisFormat, self).__init__()

        self.name = 'iis'

class ShoutcastFormat(W3cExtendedFormat):

    fields = W3cExtendedFormat.fields.copy()
    fields.update({
        'c-status': '(?P<status>\d+)',
        'x-duration': '(?P<generation_time_secs>[.\d]+)'
    })

    def __init__(self):
        super(ShoutcastFormat, self).__init__()

        self.name = 'shoutcast'

    def get(self, key):
        if key == 'user_agent':
            user_agent = super(ShoutcastFormat, self).get(key)
            return urllib.parse.unquote(user_agent)
        else:
            return super(ShoutcastFormat, self).get(key)

class AmazonCloudFrontFormat(W3cExtendedFormat):

    fields = W3cExtendedFormat.fields.copy()
    fields.update({
        'x-event': '(?P<event_action>\S+)',
        'x-sname': '(?P<event_name>\S+)',
        'cs-uri-stem': '(?:rtmp:/)?(?P<path>/\S*)',
        'c-user-agent': '(?P<user_agent>".*?"|\S+)',

        # following are present to match cloudfront instead of W3C when we know it's cloudfront
        'x-edge-location': '(?P<x_edge_location>".*?"|\S+)',
        'x-edge-result-type': '(?P<x_edge_result_type>".*?"|\S+)',
        'x-edge-request-id': '(?P<x_edge_request_id>".*?"|\S+)',
        'x-host-header': '(?P<x_host_header>".*?"|\S+)'
    })

    def __init__(self):
        super(AmazonCloudFrontFormat, self).__init__()

        self.name = 'amazon_cloudfront'

    def get(self, key):
        if key == 'event_category' and 'event_category' not in self.matched:
            return 'cloudfront_rtmp'
        elif key == 'status' and 'status' not in self.matched:
            return '200'
        elif key == 'user_agent':
            user_agent = super(AmazonCloudFrontFormat, self).get(key)
            return urllib.parse.unquote(user_agent)
        else:
            return super(AmazonCloudFrontFormat, self).get(key)

_HOST_PREFIX = '(?P<host>[\w\-\.]*)(?::\d+)?\s+'

_COMMON_LOG_FORMAT = (
    '(?P<ip>[\w*.:-]+)\s+\S+\s+(?P<userid>\S+)\s+\[(?P<date>.*?)\s+(?P<timezone>.*?)\]\s+'
    '"(?P<method>\S+)\s+(?P<path>.*?)\s+\S+"\s+(?P<status>\d+)\s+(?P<length>\S+)'
)
_NCSA_EXTENDED_LOG_FORMAT = (_COMMON_LOG_FORMAT +
    '\s+"(?P<referrer>.*?)"\s+"(?P<user_agent>.*?)"'
)
_S3_LOG_FORMAT = (
    '\S+\s+(?P<host>\S+)\s+\[(?P<date>.*?)\s+(?P<timezone>.*?)\]\s+(?P<ip>[\w*.:-]+)\s+'
    '(?P<userid>\S+)\s+\S+\s+\S+\s+\S+\s+"(?P<method>\S+)\s+(?P<path>.*?)\s+\S+"\s+(?P<status>\d+)\s+\S+\s+(?P<length>\S+)\s+'
    '\S+\s+\S+\s+\S+\s+"(?P<referrer>.*?)"\s+"(?P<user_agent>.*?)"'
)
_ICECAST2_LOG_FORMAT = ( _NCSA_EXTENDED_LOG_FORMAT +
    '\s+(?P<session_time>[0-9-]+)'
)
_ELB_LOG_FORMAT = (
    '(?P<date>[0-9-]+T[0-9:]+)\.\S+\s+\S+\s+(?P<ip>[\w*.:-]+):\d+\s+\S+:\d+\s+\S+\s+(?P<generation_time_secs>\S+)\s+\S+\s+'
    '(?P<status>\d+)\s+\S+\s+\S+\s+(?P<length>\S+)\s+'
    '"\S+\s+\w+:\/\/(?P<host>[\w\-\.]*):\d+(?P<path>\/\S*)\s+[^"]+"\s+"(?P<user_agent>[^"]+)"\s+\S+\s+\S+'
)

_OVH_FORMAT = (
    '(?P<ip>\S+)\s+' + _HOST_PREFIX + '(?P<userid>\S+)\s+\[(?P<date>.*?)\s+(?P<timezone>.*?)\]\s+'
    '"\S+\s+(?P<path>.*?)\s+\S+"\s+(?P<status>\S+)\s+(?P<length>\S+)'
    '\s+"(?P<referrer>.*?)"\s+"(?P<user_agent>.*?)"'
)

FORMATS = {
    'common': RegexFormat('common', _COMMON_LOG_FORMAT),
    'common_vhost': RegexFormat('common_vhost', _HOST_PREFIX + _COMMON_LOG_FORMAT),
    'ncsa_extended': RegexFormat('ncsa_extended', _NCSA_EXTENDED_LOG_FORMAT),
    'common_complete': RegexFormat('common_complete', _HOST_PREFIX + _NCSA_EXTENDED_LOG_FORMAT),
    'w3c_extended': W3cExtendedFormat(),
    'amazon_cloudfront': AmazonCloudFrontFormat(),
    'iis': IisFormat(),
    'shoutcast': ShoutcastFormat(),
    's3': RegexFormat('s3', _S3_LOG_FORMAT),
    'icecast2': RegexFormat('icecast2', _ICECAST2_LOG_FORMAT),
    'elb': RegexFormat('elb', _ELB_LOG_FORMAT, '%Y-%m-%dT%H:%M:%S'),
    'nginx_json': JsonFormat('nginx_json'),
    'ovh': RegexFormat('ovh', _OVH_FORMAT)
}

##
## Code.
##

class Configuration(object):
    """
    Stores all the configuration options by reading sys.argv and parsing,
    if needed, the config.inc.php.

    It has 2 attributes: options and filenames.
    """

    class Error(Exception):
        pass

    def __init__(self):
        self._parse_config()
        self._parse_args()
        self._init_config()

    def _parse_config(self):
        with open("matomo_config.yaml", 'r') as stream:
            try:
                opts =yaml.load(stream, Loader=yaml.FullLoader)
                #extract matomo parameters
                self.options = opts['Matomo_Parameters']
            except yaml.YAMLError as exc:
                logging.info(exc)
                logging.critical("Failed to parse config file. Please correct errors")
                exit(1)

    def _parse_args(self):
        """
        Parse the command line args and create self.options and self.filenames.
        """
        global ARGS
        self._args = ARGS.parse_args()
        filePath = os.path.abspath(self._args.logs)
        self.filenames  = [(filePath+"/"+x) for x in os.listdir(filePath)]
    
    def _init_config(self):
        # Configure logging
        root = logging.getLogger()        
        fmt = logging.Formatter('%(asctime)s: [%(levelname)s] %(message)s')
        fileLog = logging.FileHandler("Matomo_import.log", mode="a")
        fileLog.setLevel(logging.DEBUG if self.debug else logging.INFO)
        fileLog.setFormatter(fmt)
        root.addHandler(fileLog)
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if self.debug else logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)
        #override matomo config vars, where applicable
        root.setLevel(logging.DEBUG if self.debug else logging.INFO)
        logging.debug("Initialized")
        if self.server:
            logging.debug(f"Using {self._args.server} as matomo url (override)")
            self.options["matomo_url"] = self.server

    def __getattr__(self, name):
        return getattr(self._args, name, self.options.get(name, None))

class UrlHelper(object):

    @staticmethod
    def convert_array_args(args):
        """
        Converts PHP deep query param arrays (eg, w/ names like hsr_ev[abc][0][]=value) into a nested list/dict
        structure that will convert correctly to JSON.
        """

        final_args = {}
        for key, value in args.items():
            indices = key.split('[')
            if '[' in key:
                # contains list of all indices, eg for abc[def][ghi][] = 123, indices would be ['abc', 'def', 'ghi', '']
                indices = [i.rstrip(']') for i in indices]

                # navigate the multidimensional array final_args, creating lists/dicts when needed, using indices
                element = final_args
                for i in range(0, len(indices) - 1):
                    idx = indices[i]

                    # if there's no next key, then this element is a list, otherwise a dict
                    element_type = list if not indices[i + 1] else dict
                    if idx not in element or not isinstance(element[idx], element_type):
                        element[idx] = element_type()

                    element = element[idx]

                # set the value in the final container we navigated to
                if not indices[-1]: # last indice is '[]'
                    element.append(value)
                else: # last indice has a key, eg, '[abc]'
                    element[indices[-1]] = value
            else:
                final_args[key] = value

        return UrlHelper._convert_dicts_to_arrays(final_args)

    @staticmethod
    def _convert_dicts_to_arrays(d):
        # convert dicts that have contiguous integer keys to arrays
        for key, value in d.items():
            if not isinstance(value, dict):
                continue

            if UrlHelper._has_contiguous_int_keys(value):
                d[key] = UrlHelper._convert_dict_to_array(value)
            else:
                d[key] = UrlHelper._convert_dicts_to_arrays(value)

        return d

    @staticmethod
    def _has_contiguous_int_keys(d):
        for i in range(0, len(d)):
            if str(i) not in d:
                return False
        return True

    @staticmethod
    def _convert_dict_to_array(d):
        result = []
        for i in range(0, len(d)):
            result.append(d[str(i)])
        return result


class Matomo(object):
    """
    Make requests to Matomo.
    """
    def __init__(self):
        self.url = config.matomo_url
        self._send_request = self._real_request
        #check if we should really send
        if config.dry_run:
            logging.info("Doing dry run")
            self._send_request = self._fake_request

    class Error(Exception):

        def __init__(self, message, code = None):
            super(Exception, self).__init__(message)

            self.code = code

    class RedirectHandlerWithLogging(urllib.request.HTTPRedirectHandler):
        """
        Special implementation of HTTPRedirectHandler that logs redirects in debug mode
        to help users debug system issues.
        """

        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            logging.debug("Request redirected (code: %s) to '%s'" % (code, newurl))
            return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, hdrs, newurl)

    def _fake_request(self, request, data):
        logging.info(f"Would send {request.get_method()} - {request.full_url} with {len(request.data)} bytes with {len(json.loads(data).get('requests', []))} requests. Timeout {config.options.get('default_socket_timeout', 'None')}")
        return b"{}"

    def _real_request(self, request, data):
        # Use non-default SSL context if invalid certificates shall be
        # accepted.
        https_handler_args = {}
        opener = urllib.request.build_opener(
            Matomo.RedirectHandlerWithLogging(),
            urllib.request.HTTPSHandler(**https_handler_args))
        response = opener.open(request, timeout = config.options.get("default_socket_timeout", None))
        result = response.read()
        response.close()
        return result

    def _call(self, path, args, headers=None, url=None, data=None):
        """
        Make a request to the Matomo site. It is up to the caller to format
        arguments, to embed authentication, etc.
        """
        url = url or self.url
        headers = headers or {}

        if data is None:
            # If Content-Type isn't defined, PHP do not parse the request's body.
            headers['Content-type'] = 'application/x-www-form-urlencoded'
            data = urllib.parse.urlencode(args)
        elif not isinstance(data, str) and headers['Content-type'] == 'application/json':
            data = json.dumps(data).encode('utf-8')
            if args:
                path = path + '?' + urllib.parse.urlencode(args)

        headers['User-Agent'] = 'Matomo/LogImport'

        request = urllib.request.Request(url + path, data, headers)

        return self._send_request(request, data)

    def _call_api(self, method, **kwargs):
        """
        Make a request to the Matomo API taking care of authentication, body
        formatting, etc.
        """
        args = {
            'module' : 'API',
            'format' : 'json2',
            'method' : method,
            'filter_limit' : '-1',
        }
        if kwargs:
            args.update(kwargs)

        # Convert lists into appropriate format.
        # See: http://developer.matomo.org/api-reference/reporting-api#passing-an-array-of-data-as-a-parameter
        # Warning: we have to pass the parameters in order: foo[0], foo[1], foo[2]
        # and not foo[1], foo[0], foo[2] (it will break Matomo otherwise.)
        final_args = []
        for key, value in args.items():
            if isinstance(value, (list, tuple)):
                for index, obj in enumerate(value):
                    final_args.append(('%s[%d]' % (key, index), obj))
            else:
                final_args.append((key, value))


        logging.debug(f"Arguments: {final_args}")

        res = self._call('/', final_args)

        try:
            return json.loads(res.decode('utf-8'))
        except ValueError:
            raise urllib.error.URLError('Matomo returned an invalid response: ' + res)

    def _call_wrapper(self, func, expected_response, on_failure, *args, **kwargs):
        """
        Try to make requests to Matomo at most MATOMO_FAILURE_MAX_RETRY times.
        """
        errors = 0
        while True:
            try:
                response = func(*args, **kwargs)
                if expected_response is not None and response != expected_response:
                    if on_failure is not None:
                        error_message = on_failure(response, kwargs.get('data'))
                    else:
                        error_message = "didn't receive the expected response. Response was %s " % response

                    raise urllib.error.URLError(error_message)
                return response
            except (urllib.error.URLError, http.client.HTTPException, ValueError, socket.timeout) as e:
                logging.warning('Error when connecting to Matomo: %s', e)

                code = None
                if isinstance(e, urllib.error.HTTPError):
                    # See Python issue 13211.
                    message = 'HTTP Error %s %s' % (e.code, e.msg)
                    code = e.code
                elif isinstance(e, urllib.error.URLError):
                    message = e.reason
                else:
                    message = str(e)

                # decorate message w/ HTTP response, if it can be retrieved
                if hasattr(e, 'read'):
                    message = message + ", response: " + str(e.read())

                try:
                    delay_after_failure = config.options["delay_after_failure"]
                    max_attempts = config.options["default_max_attempts"]
                except NameError:
                    delay_after_failure = MATOMO_DEFAULT_DELAY_AFTER_FAILURE
                    max_attempts = MATOMO_DEFAULT_MAX_ATTEMPTS

                errors += 1
                if errors < max_attempts:
                    logging.error("Retrying request, attempt number %d" % (errors + 1))
                    time.sleep(delay_after_failure)
                else:
                    logging.critical("Max number of attempts reached, server is unreachable!")
                    raise Matomo.Error(message, code)

    def call(self, path, args, expected_content=None, headers=None, data=None, on_failure=None):
        return self._call_wrapper(self._call, expected_content, on_failure, path, args, headers,
                                    data=data)

    def call_api(self, method, **kwargs):
        return self._call_wrapper(self._call_api, None, None, method, **kwargs)

class Recorder(object):
    """
    A Recorder fetches hits from the Queue and inserts them into Matomo using
    the API.
    """

    recorders = []
    queue = queue.Queue()

    def __init__(self):
        self.hits = []
        logging.debug(f"Max-Payload Size: {config.max_payload}")
        self.threshold = config.max_payload or 200


    @classmethod
    def launch(cls, recorder_count):
        """
        Launch a bunch of Recorder objects in a separate thread.
        """
        for i in range(recorder_count):
            recorder = Recorder()
            recorder.nbr = i
            cls.recorders.append(recorder)

            t = threading.Thread(target=recorder._run_bulk)

            t.daemon = True
            t.start()
            logging.debug(f'Launched recorder {i} with threshold {recorder.threshold}')

    @classmethod
    def add_hit(cls, hit):
        cls.queue.put(hit)

    @classmethod
    def add_hits(cls, all_hits):
        """
        Add a set of hits to the recorders queue.
        """
        for hit in all_hits:
            cls.queue.put(hit)

    @classmethod
    def wait_empty(cls):
        """
        Wait until all recorders have an empty queue.
        """
        #push none to signal final cleanup
        logging.debug("Waiting for recorders to end")
        for i in cls.recorders:
            cls.queue.put(None)
        #then end
        while not cls.queue.empty() and not state.is_stopped:
            sleep(0.5)

    def _run_bulk(self):
        while True:
            try:
                hit = Recorder.queue.get()
                if hit is None:
                    logging.debug(f"Terminate recorder {self.nbr}")
                    #this recorder should terminate now
                    #process remaining hits
                    self._record_hits()
                    break
                else:
                    self.hits.append(hit)
                    if len(self.hits) >= self.threshold:
                        logging.debug("Trigger transport")
                        self._record_hits()

            except Matomo.Error as e:
                logging.critical(f"Error {e}")
                logging.debug("Following hits where present:\n" + '\n'.join(f"{h.filename} -> {h.lineno}" for h in self.hits))
                state.stop(f"Encountered error {e} in file {self.hits[0].filename}")
                #terminate the loop
                break
            except Exception as e:
                import traceback
                logging.error(f"Failed to process hit: {e}")
                traceback.print_exc(file=sys.stderr)
                # TODO: we should log something here, however when this happens, logging.etc will throw
                state.stop()
                break
            finally:
                #always end the loop
                #this includes the break statement in `if hit is None:` case
                Recorder.queue.task_done()
        logging.debug(f"Recorder {self.nbr} finished")

    def date_to_matomo(self, date):
        date, time = date.isoformat(sep=' ').split()
        return '%s %s' % (date, time.replace('-', ':'))

    def _get_hit_args(self, hit):
        """
        Returns the args used in tracking a hit, without the token_auth.
        """
        #site_id, main_url = resolver.resolve(hit)
        site_id = config.options["idSite"]
        #repositoy base url
        main_url = config.options["repository_base_url"]

        #stats.dates_recorded.add(hit.date.date())

        path = hit.path

        # only prepend main url / host if it's a path
        url_prefix = self._get_host_with_protocol(hit.host, main_url) if hasattr(hit, 'host') else main_url
        url = (url_prefix if path.startswith('/') else '') + path[:1024]

        if (hit.referrer.find("?") >=0):
            hit.referrer = hit.referrer.split("?")[0]+" "

        args = {
            'rec': '1',
            'apiv': '1',
            'url': url,
            'urlref': hit.referrer[:1024],
            'cip': hit.ip,
            'cdt': self.date_to_matomo(hit.date),
            'idsite': site_id,
            'ua': hit.user_agent
        }

        # idsite is already determined by resolver
        if 'idsite' in hit.args:
            del hit.args['idsite']
            
        args.update(hit.args)

        if hit.is_download:
            args['download'] = args['url']

        args['bots'] = '1'

        if hit.generation_time_milli > 0:
            args['gt_ms'] = int(hit.generation_time_milli)

        if hit.event_category and hit.event_action:
            args['e_c'] = hit.event_category
            args['e_a'] = hit.event_action

            if hit.event_name:
                args['e_n'] = hit.event_name

        if hit.length:
            args['bw_bytes'] = hit.length

        # convert custom variable args to JSON
        if 'cvar' in args and not isinstance(args['cvar'], str):
            args['cvar'] = json.dumps(args['cvar'])

        if '_cvar' in args and not isinstance(args['_cvar'], str):
            args['_cvar'] = json.dumps(args['_cvar'])

        return UrlHelper.convert_array_args(args)

    def _get_host_with_protocol(self, host, main_url):
        if '://' not in host:
            parts = urllib.parse.urlparse(main_url)
            host = parts.scheme + '://' + host
        return host

    def _record_hits(self):
        """
        Inserts several hits into Matomo.
        """
        #check if we need to do something
        if len(self.hits) == 0:
            logging.debug("Worker already depleted -> shutting down directly")
            return
        
        data = {
            'token_auth': config.options["token_auth"],
            'requests': [self._get_hit_args(hit) for hit in self.hits]
        }

        try:
            args = {}
 
            response = matomo.call(
                '/piwik.php', args=args,
                expected_content=None,
                headers={'Content-type': 'application/json'},
                data=data,
                on_failure=self._on_tracking_failure
            )
            # check for invalid requests
            try:
                response = json.loads(response.decode('utf-8'))
            except:
                logging.info("bulk tracking returned invalid JSON")

                response = {}

            if ('invalid_indices' in response and isinstance(response['invalid_indices'], list) and
                response['invalid_indices']):
                invalid_count = len(response['invalid_indices'])

                invalid_lines = [str(self.hits[index].lineno) for index in response['invalid_indices']]
                invalid_lines_str = ", ".join(invalid_lines)

                #stats.invalid_lines.extend(invalid_lines)

                logging.info("The Matomo tracker identified %s invalid requests on lines: %s" % (invalid_count, invalid_lines_str))
            elif 'invalid' in response and response['invalid'] > 0:
                logging.info("The Matomo tracker identified %s invalid requests." % response['invalid'])
        except Matomo.Error as e:
            # if the server returned 400 code, BulkTracking may not be enabled
            if e.code == 400:
                logging.critical("Server returned status 400 (Bad Request).\nIs the BulkTracking plugin disabled?")

            raise
        #increment stats
        stats.count_lines_recorded.advance(len(self.hits))
        #reset hits
        self.hits.clear()


    def _on_tracking_failure(self, response, data):
        """
        Removes the successfully tracked hits from the request payload so
        they are not logged twice.
        """
        try:
            response = json.loads(response.decode('utf-8'))
        except:
            # the response should be in JSON, but in case it can't be parsed just try another attempt
            logging.debug("cannot parse tracker response, should be valid JSON")
            return response

        # remove the successfully tracked hits from payload
        tracked = response['tracked']
        data['requests'] = data['requests'][tracked:]

        return response['message']

class Hit(object):
    """
    It's a simple container.
    """
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        super(Hit, self).__init__()


    def get_visitor_id_hash(self):
        visitor_id = self.ip
        return abs(hash(visitor_id))

    def add_page_custom_var(self, key, value):
        """
        Adds a page custom variable to this Hit.
        """
        self._add_custom_var(key, value, 'cvar')

    def add_visit_custom_var(self, key, value):
        """
        Adds a visit custom variable to this Hit.
        """
        self._add_custom_var(key, value, '_cvar')

    def _add_custom_var(self, key, value, api_arg_name):
        if api_arg_name not in self.args:
            self.args[api_arg_name] = {}

        if isinstance(self.args[api_arg_name], str):
            logging.debug("Ignoring custom %s variable addition [ %s = %s ], custom var already set to string." % (api_arg_name, key, value))
            return

        index = len(self.args[api_arg_name]) + 1
        self.args[api_arg_name][index] = [key, value]

class CheckRobots(object):
    ROBOT_LIST = "robots.json"
    def _ensureFile(self):
        if os.access(CheckRobots.ROBOT_LIST, os.R_OK):
            return
        url = config.options["COUNTER_Robots_url"]
        response = urllib.request.urlopen(url)
        with open(CheckRobots.ROBOT_LIST, 'w') as json_file:
            json_file.write(response.read().decode("utf-8"))
        
    def _readCOUNTERRobots(self):
        self._ensureFile()
        with open(CheckRobots.ROBOT_LIST) as json_file:
            self.counterRobotsList = json.load(json_file)
        return self.counterRobotsList

    def __init__(self):
        self._readCOUNTERRobots()


MONTHS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4,
          'May': 5, 'Jun': 6, 'Jul': 7, 'Aug': 8,
          'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}    

class Parser(object):
    """
    The Parser parses the lines in a specified file and inserts them into
    a Queue.
    """

    def __init__(self):
        self.check_methods = [method for name, method
                              in inspect.getmembers(self, predicate=inspect.ismethod)
                              if name.startswith('check_')]
        self.gen_matcher()

    def gen_matcher(self):
        #tracking_metadata
        self.tracking_metadata = []
        if config.options["tracking_metadata"] is not None:
            for i in config.options["tracking_metadata"]:
                pattern = re.compile(i)
                self.tracking_metadata.append(pattern)
        #tracking_download
        self.tracking_download = []
        if config.options["tracking_download"] is not None:
                for i in config.options["tracking_download"]:
                    pattern = re.compile(i)
                    self.tracking_download.append(pattern)
        #user_agents
        self.user_agents = []
        for p in checkRobots.counterRobotsList:
            self.user_agents.append(re.compile(p["pattern"]))


    ## All check_* methods are called for each hit and must return True if the
    ## hit can be imported, False otherwise.

    def check_static(self, hit):
        for regEx in self.tracking_metadata:
            oai = regEx.match(hit.path)
            if not oai is None:
                finalOAIpmh=config.options["oaipmh_preamble"]+oai.group(1)[oai.group(1).rfind("/")+1:]
                if finalOAIpmh!=config.options["oaipmh_preamble"]:
                    hit.add_page_custom_var("oaipmhID",finalOAIpmh)
                    hit.is_meta=True
        return True

    def check_download(self, hit):
        for regEx in self.tracking_download:
            oai = regEx.match(hit.path)
            if not oai is None:
                finalOAIpmh=config.options["oaipmh_preamble"]+oai.group(1)[oai.group(1).rfind("/")+1:]
                if finalOAIpmh!=config.options["oaipmh_preamble"]:
                    hit.add_page_custom_var("oaipmhID",finalOAIpmh)
                    hit.is_download = True
                    break
        return True

    def check_user_agent(self, hit):
        for robot in self.user_agents:
            if robot.search(hit.user_agent):
                stats.count_lines_skipped_user_agent.increment()
                hit.is_robot = True
                break
        return True

    def check_http_error(self, hit):
        if 4 <= int(hit.status[0]) <= 5:
            hit.is_error = True
        return True

    def check_http_redirect(self, hit):
        if hit.status[0] == '3' and hit.status != '304':
             hit.is_redirect = True
        return True

    @staticmethod
    def check_format(lineOrFile):
        format = False
        format_groups = 0
        for name, candidate_format in FORMATS.items():
            logging.debug("Check format %s", name)

            # skip auto detection for formats that can't be detected automatically
            if name == 'ovh':
                continue

            match = None
            try:
                if isinstance(lineOrFile, str):
                    match = candidate_format.check_format_line(lineOrFile)
                else:
                    match = candidate_format.check_format(lineOrFile)
            except Exception as e:
                logging.debug('Error in format checking: %s', traceback.format_exc())
                pass

            if match:
                logging.debug('Format %s matches', name)

                # compare format groups if this *BaseFormat has groups() method
                try:
                    # if there's more info in this match, use this format
                    match_groups = len(match.groups())

                    logging.debug('Format match contains %d groups' % match_groups)

                    if format_groups < match_groups:
                        format = candidate_format
                        format_groups = match_groups
                except AttributeError:
                    format = candidate_format

            else:
                logging.debug('Format %s does not match', name)

        # if the format is W3cExtendedFormat, check if the logs are from IIS and if so, issue a warning if the
        # --w3c-time-taken-milli option isn't set
        if isinstance(format, W3cExtendedFormat):
            format.check_for_iis_option()
        #print "Format name "+format.name
        return format

    @staticmethod
    def detect_format(file):
        """
        Return the best matching format for this file, or None if none was found.
        """
        logging.debug('Detecting the log format')

        format = False

        # check the format using the file (for formats like the W3cExtendedFormat one)
        format = Parser.check_format(file)
        # check the format using the first N lines (to avoid irregular ones)
        lineno = 0
        limit = 100000
        while not format and lineno < limit:
            line = file.readline()
            if not line: # if at eof, don't keep looping
                break

            lineno = lineno + 1

            logging.debug("Detecting format against line %i" % lineno)
            format = Parser.check_format(line)

        try:
            file.seek(0)
        except IOError:
            pass

        if not format:
            logging.critical("cannot automatically determine the log format using the first %d lines of the log file. " % limit +
                        "\nMaybe try specifying the format with the --log-format-name command line argument." )
            return

        logging.debug('Format %s is the best match', format.name)
        return format

    def is_filtered(self, hit):
        host = None
        if hasattr(hit, 'host'):
            host = hit.host
        else:
            try:
                host = urllib.parse.urlparse(hit.path).hostname
            except:
                pass
        return (False, None)

    def parse(self, filename):
        """
        Parse the specified filename and insert hits in the queue.
        """
        def invalid_line(line, reason):
            logging.debug('Invalid line detected (%s): %s' % (reason, line))

        def filtered_line(line, reason):
            logging.debug('Filtered line out (%s): %s' % (reason, line))

        if filename == '-':
            filename = '(stdin)'
            file = sys.stdin
        else:
            if not os.path.exists(filename):
                logging.warning(f"=====> File {file} does not exist <=====")
                return
            else:
                if filename.endswith('.bz2'):
                    open_func = bz2.BZ2File
                elif filename.endswith('.gz'):
                    open_func = gzip.open
                else:
                    open_func = open
                import io
                file = io.TextIOWrapper(open_func(filename, 'rb'))


        format = self.detect_format(file)
        if format is None:
            return logging.critical(
                'Cannot guess the logs format. Please give one using '
                'either the --log-format-name or --log-format-regex option'
            )
        # Make sure the format is compatible with the resolver.
        #resolver.check_format(format)
        valid_lines_count = 0

        hits = []
        lineno = -1
        while not state.is_stopped:
            line = file.readline()

            if not line: break
            lineno = lineno + 1


            stats.count_lines_parsed.increment()
            if stats.count_lines_parsed.value <= config.skip:
                continue

            match = format.match(line)
            if not match:
                invalid_line(line, 'line did not match')
                continue

            valid_lines_count = valid_lines_count + 1
            try:
                format_status = format.get('status')
            except:
                format_status = ""
            try:
                format_path = format.get('path')
            except:
                format_path = ""

            hit = Hit(
                filename=filename,
                lineno=lineno,
                status=format_status,
                full_path=format_path,
                is_meta=False,
                is_download=False,
                is_robot=False,
                is_error=False,
                is_redirect=False,
                args={},
            )

            # W3cExtendedFormat detaults to - when there is no query string, but we want empty string
            hit.query_string = ''
            hit.path = hit.full_path

            try:
                hit.referrer = format.get('referrer')

                if hit.referrer.startswith('"'):
                    hit.referrer = hit.referrer[1:-1]
            except BaseFormatException:
                hit.referrer = ''
            if hit.referrer == '-':
                hit.referrer = ''

            try:
                hit.user_agent = format.get('user_agent')

                # in case a format parser included enclosing quotes, remove them so they are not
                # sent to Matomo
                if hit.user_agent.startswith('"'):
                    hit.user_agent = hit.user_agent[1:-1]
            except BaseFormatException:
                hit.user_agent = ''

            hit.ip = format.get('ip')

            #IP anonymization
            if config.options["ip_anonymization"]:
                ip0, ip1, *_ = hit.ip.split('.')
                hit.ip = f"{ip0}.{ip1}.0.0"

            try:
                hit.length = int(format.get('length'))
            except (ValueError, BaseFormatException):
                # Some lines or formats don't have a length (e.g. 304 redirects, W3C logs)
                hit.length = 0

            try:
                hit.generation_time_milli = float(format.get('generation_time_milli'))
            except (ValueError, BaseFormatException):
                try:
                    hit.generation_time_milli = float(format.get('generation_time_micro')) / 1000
                except (ValueError, BaseFormatException):
                    try:
                        hit.generation_time_milli = float(format.get('generation_time_secs')) * 1000
                    except (ValueError, BaseFormatException):
                        hit.generation_time_milli = 0

            try:
                hit.host = format.get('host').lower().strip('.')
                if hit.host.startswith('"'):
                    hit.host = hit.host[1:-1]
            except BaseFormatException:
                # Some formats have no host.
                pass

            # Add userid
            try:
                hit.userid = None
                userid = format.get('userid')
                if userid != '-':
                    hit.args['uid'] = hit.userid = userid
            except:
                pass

            # add event info
            try:
                hit.event_category = hit.event_action = hit.event_name = None

                hit.event_category = format.get('event_category')
                hit.event_action = format.get('event_action')

                hit.event_name = format.get('event_name')
                if hit.event_name == '-':
                    hit.event_name = None
            except:
                pass

            # Check if the hit must be excluded.
            if not all((method(hit) for method in self.check_methods)):
                continue

            # Parse date.
            # We parse it after calling check_methods as it's quite CPU hungry, and
            # we want to avoid that cost for excluded hits.
            date_string = format.get('date')

            try:
                hit.date = format.parseTime(date_string)
            except ValueError as e:
                invalid_line(line, 'invalid date or invalid format: %s' % str(e))
                continue

            # Parse timezone and substract its value from the date
            try:
                timezone = float(format.get('timezone'))
            except BaseFormatException:
                timezone = 0
            except ValueError:
                invalid_line(line, 'invalid timezone')
                continue

            if timezone:
                hit.date -= datetime.timedelta(hours=timezone/100)

            (is_filtered, reason) = self.is_filtered(hit)
            if is_filtered:
                filtered_line(line, reason)
                continue
            if (not hit.is_robot) and (hit.is_meta or hit.is_download) and (not hit.is_redirect):
                Recorder.add_hit(hit)
            if (not hit.is_robot and not hit.is_redirect and hit.is_meta):
                stats.count_lines_static.increment()
            if (not hit.is_robot and not hit.is_redirect and hit.is_download):
                stats.count_lines_downloads.increment()

class Statistics(object):
    """
    Store statistics about parsed logs and recorded entries.
    Can optionally print statistics on standard output every second.
    """

    class Counter(object):
        """
        Simple integers cannot be used by multithreaded programs. See:
        http://stackoverflow.com/questions/6320107/are-python-ints-thread-safe
        """
        def __init__(self):
            # itertools.count's implementation in C does not release the GIL and
            # therefore is thread-safe.
            self.counter = itertools.count(1)
            self.value = 0

        def increment(self):
            self.value = next(self.counter)

        def advance(self, n):
            for i in range(n):
                self.increment()

        def __str__(self):
            return str(int(self.value))

    def __init__(self):
        self.time_start = None
        self.time_stop = None

        self.count_lines_parsed = self.Counter()
        self.count_lines_recorded = self.Counter()

        # requests that the Matomo tracker considered invalid (or failed to track)
        self.invalid_lines = []

        # Do not match the regexp.
        self.count_lines_invalid = self.Counter()
        # Were filtered out.
        self.count_lines_filtered = self.Counter()
        # Static files.
        self.count_lines_static = self.Counter()
        # Ignored user-agents.
        self.count_lines_skipped_user_agent = self.Counter()
        # Downloads
        self.count_lines_downloads = self.Counter()

        # Misc
        self.dates_recorded = set()
        self.monitor_stop = False

    def set_time_start(self):
        self.time_start = time.time()

    def set_time_stop(self):
        self.time_stop = time.time()

    def _compute_speed(self, value, start, end):
        delta_time = end - start
        if value == 0:
            return 0
        if delta_time == 0:
            return 'very high!'
        else:
            return value / delta_time

    def _round_value(self, value, base=100):
        return round(value * base) / base

    def _indent_text(self, lines, level=1):
        """
        Return an indented text. 'lines' can be a list of lines or a single
        line (as a string). One level of indentation is 4 spaces.
        """
        prefix = ' ' * (4 * level)
        if isinstance(lines, str):
            return prefix + lines
        else:
            return '\n'.join(
                prefix + line
                for line in lines
            )

    def print_summary(self):
        invalid_lines_summary = ''
        if self.invalid_lines:
            invalid_lines_summary = '''Invalid log lines
-----------------

The following lines were not tracked by Matomo, either due to a malformed tracker request or error in the tracker:

%s

''' % textwrap.fill(", ".join(self.invalid_lines), 80)

        logging.info('''
%(invalid_lines)sLogs import summary
-------------------

    %(count_lines_recorded)d requests imported successfully
    %(count_lines_downloads)d requests were downloads
    %(count_lines_metadata)d requests were metadata
    %(count_lines_skipped_user_agent)d requests ignored done by bots, search engines...

Performance summary
-------------------

    Total time: %(total_time)d seconds
    Requests imported per second: %(speed_recording)s requests per second


''' % {

    'count_lines_recorded': self.count_lines_recorded.value,
    'count_lines_downloads': self.count_lines_downloads.value,
    'count_lines_metadata': self.count_lines_static.value,
    'count_lines_skipped_user_agent': self.count_lines_skipped_user_agent.value,
    'total_time': self.time_stop - self.time_start,
    'speed_recording': self._round_value(self._compute_speed(
            self.count_lines_recorded.value,
            self.time_start, self.time_stop,
        )),
    'invalid_lines': invalid_lines_summary
})

    ##
    ## The monitor is a thread that prints a short summary each second.
    ##

    def _monitor(self):
        latest_total_recorded = 0
        while not state.is_stopped:
            current_total = stats.count_lines_recorded.value
            time_elapsed = time.time() - self.time_start

            logging.info('%d lines parsed, %d lines recorded, %d records/sec (avg), %d records/sec (current)' % (
                stats.count_lines_parsed.value,
                current_total,
                current_total / time_elapsed if time_elapsed != 0 else 0,
                current_total - latest_total_recorded,
            ))
            latest_total_recorded = current_total
            time.sleep(1)

    def start_monitor(self):
        t = threading.Thread(target=self._monitor)
        t.daemon = True
        t.start()

class State:
    def __init__(self):
        self._stopped = True
        self._reason = None
        self._lock = threading.Lock()

    @property
    def is_stopped(self):
        with self._lock:
            return self._stopped

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def start(self):
        with self._lock:
            self._stopped = False

    def stop(self, reason = None):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._reason = reason

def main():
    """
    Start the importing process.
    """
    state.start()
    stats.set_time_start()
    stats.start_monitor()
    Recorder.launch(config.options["recorders"])

    try:
        for filename in config.filenames:
            if state.is_stopped:
                break
            logging.info(f"Reading {filename} ...")
            parser.parse(filename)

        Recorder.wait_empty()
    except KeyboardInterrupt:
        state.stop("Interrupted")
        pass
    finally:
        state.stop()
        stats.set_time_stop()
        stats.print_summary()
        if state.reason:
            logging.info("Programm ending with reason:")
            logging.info(state.reason)

if __name__ == '__main__':
    try:
        #state = State()
        config = Configuration()
        checkRobots = CheckRobots()
        #matomo = Matomo()
        #stats = Statistics()
        #parser = Parser()
        #main()
    except KeyboardInterrupt:
        pass
