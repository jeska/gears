#!/usr/bin/python

import fnmatch
import httplib
import os
import os.path
import re
import simplejson
import urllib
from decimal import Decimal
from optparse import OptionParser

from bencode import bdecode, bencode

url = "/transmission/rpc"
host = "localhost"
port = "9091"

class Gears:
    # taken from libtransmission/transmission.h
    tr_status = {
        1 << 0: 'waiting to check',
        1 << 1: 'checking',
        1 << 2: 'downloading',
        1 << 3: 'seeding',
        1 << 4: 'stopped',
    }

    class MethodException(Exception):
        pass

    class QueryException(Exception):
        pass

    def __init__(self, **kw):
        self.connect()
        self.torrents = {}

        self.debug_level = kw.get('debug_level', 0)

    def connect(self):
        self.h = httplib.HTTPConnection(host, port, True)
        self.h.connect()

    def send_message(self, method, **method_arguments):
        # generate transmission message
        d = dict(
            method = method,
            arguments = method_arguments,
        )

        # convert to JSON
        message = simplejson.dumps(d)
        
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        if self.debug_level:
            print ">> %s" % message

        self.h.request("POST", url, message, headers)

        return self.read_message()


    def read_message(self):
        r = self.h.getresponse()

#        if debug >= 2:
#            print "<< %s" % r.read()

        return r

    def get_torrent_info(self):
        # if self.torrents is already populated, return
        if len(self.torrents) > 0:
            return

        response = self.send_message("torrent-get", fields = ['id', 'name',
            'error', 'errorString', 'eta', 'rateDownload', 'rateUpload',
            'status', 'sizeWhenDone', 'totalSize', 'uploadRatio',
            'downloadEver', 'uploadEver' ],
        )

        # parse JSON 
        result = response.read()
        result = simplejson.loads(result)

        result = result['arguments']['torrents']

        torrents = []
        for t in result:
            # add some useful keys
            t['status-actual'] = t['status']
            t['status'] = self.tr_status[t['status']]

            # add the torrent 
            torrents.append(t)

        self.torrents = torrents

        return

    def do(self, method, torrents):
        ids = [ t['id'] for t in torrents ]

        response = self.send_message(method, ids = ids)

        response = response.read()
        response = simplejson.loads(response)
        if response['result'] != 'success':
            raise self.MethodException(response['result'])

        return True

    def add_torrent(self, torrent_file):
        response = self.send_message("torrent-add", filename = torrent_file)

        response = response.read()
        response = simplejson.loads(response)
        if response['result'] != 'success':
            # FIXME
            raise self.MethodException("Could not add torrent '%s': %s" % \
                (torrent_file, response['result']))

        return True

    def remove_torrents(self, torrents):
        try:
            self.do("torrent-remove", torrents)
        except self.MethodException, e:
            raise self.MethodException("Could not remove torrents: %s", e)

        return True

    def start_torrents(self, torrents):
        try:
            self.do("torrent-start", torrents)
        except self.MethodException, e:
            raise self.MethodException("Could not start torrents: %s", e)

        return True

    def stop_torrents(self, torrents):
        try:
            self.do("torrent-stop", torrents)
        except self.MethodException, e:
            raise self.MethodException("Could not stop torrents: %s", e)

        return True

    def verify_torrents(self, torrents):
        try:
            self.do("torrent-verify", torrents)
        except self.MethodException, e:
            raise self.MethodException("Could not verify torrents: %s", e)

        return True

    def parse_query(self, query):
        class FilterFalseException(Exception):
            pass

        if not query:
            return {}

        # make sure query is an array
        if isinstance(query, basestring):
            query = query.split()

        # regex for regex flags in regex filters (horrible comment...)
        flags_re = r'''
            (?<!\\)     # don't match if preceeded by a backslash
            /           # flags follow a forward slash
            ([ilmsux]+) # group the flags
            \Z          # end of string
        '''
        flags_re = re.compile(flags_re, re.X | re.I)

        # regex for the filters themselves
        filter_re = r'''
            ([A-Za-z-]+) # key
            (!)?         # negation of immediately following operator
            ([=~<>])     # operator
                         #     = equality (if "*" and "?" are in the
                         #         value, switch to globbing mode)
                         #     ~ regex
                         #     < less than
                         #     > greater than
            ([^ ]+)      # value
        '''
        filter_re = re.compile(filter_re, re.X)

        torrent_matches = []
        filters = {}
        for arg in query:
            m = filter_re.search(arg)

            # match the name by default
            while not m:
                arg = "name=%s" % arg
                m = re.compile(filter_re, re.X).search(arg)

            key, negation, operator, value = m.groups()

            # The "lambda v, value=value: ..." uglyness is needed so that the
            # current value of "value" is used as the filter value. If we
            # didn't have this, "value", when actually executing the filters
            # below, would always be the last filter value in the argument
            # string.
            #
            # To elaborate, if our filters were "state=see* ratio>0.5",
            # without the ugly hack above, "value = 0.5" for all executions
            # regardless of filter function. This breaks things and isn't
            # correct. The behavior we want is for "value" to be different for
            # each filter function, and that's the behavior we try to have by
            # having the "lambda v, value=value: ..." uglyness.

            # get the proper filtering function
            if operator == '=':
                # if there are globbing metacharaters, glob-match
                if re.search('[*?[]', value):
                    f = lambda v, value=value: fnmatch.fnmatch(v, value) 
                # otherwise, perform a strict equality match
                else:
                    f = lambda v, value=value: v == value
            elif operator == '~':
                # if there are regex flags at the end of the pattern, compile
                # them into the regex
                m = flags_re.search(value)
                if m:
                    flags_str = m.group(1)
                    for f in flags_str:
                        # get the actual re.[ILMSUX] value for the flag
                        f = eval("re.%s" % f.upper())

                        # add it to the flags
                        try:
                            flags = flags | f
                        except UnboundLocalError:
                            flags = f

                    # remove the flags to get the actual pattern
                    value = flags_re.sub('', value)
                try:
                    regex = re.compile(value, flags)
                except UnboundLocalError:
                    regex = re.compile(value)

                f = lambda v, regex=regex: regex.search(v)
            elif operator == '>':
                try:
                    value = float(value)
                except ValueError:
                    parser.error("invalid filter value")

                f = lambda v, value=value: float(v) > value
            elif operator == '<':
                try:
                    value = float(value)
                except ValueError:
                    parser.error("invalid filter value")

                f = lambda v, value=value: float(v) < value
            else:
                raise QueryException("invalid query")

            if negation is not None:
                # "f=f" uglyness is so that we use the current value of "f"
                # instead of causing, um, infinite recursion
                f = lambda v, f=f: not f(v)

            filters[key] = f

        self.get_torrent_info()

        # run each filter against each torrent
        for t in self.torrents:
            try:
                for k, filter in filters.iteritems(): 
                    try:
                        if not filter(t[k]):
                            raise FilterFalseException
                    except KeyError:
                        raise self.QueryException("invalid filter key")
            except FilterFalseException:
                continue
            else:
                torrent_matches.append(t)

        return torrent_matches

if __name__ == '__main__':
    def dry_run_callback(option, opt, value, parser):
        parser.values.dry_run = True
        parser.values.debug_level = 1

    parser = OptionParser(usage = "usage: %prog [options] command [args]")
    parser.add_option("-0", action = "store_const", const = "\0", 
                      dest = "record_separator")
    parser.add_option("-H", "--hashes", action = "store_const", 
                      const = "%hash", dest = "output_format")
    parser.add_option("-n", "--dry-run", action = "callback", 
                      callback = dry_run_callback, dest = "dry_run",
                      default = False)
    parser.add_option("-o", "--output_format", dest = "output_format", 
                      default = "%name")
    parser.add_option("--rs", dest = "record_separator", default = "\n")
    parser.add_option("-v", "--verbose", action = "count", 
                      dest = "debug_level", default = 0)

    (options, args) = parser.parse_args()

    # make the output format into a printf-friendly string
    options.output_format = re.sub(r'%([A-Za-z-]+)', r'%(\1)s', options.output_format)

    if options.dry_run:
        print "dry run: no changes will be made"

    try:
        cmd = args[0]
        args = args[1:]
    except IndexError:
        parser.error("incorrect number of arguments")

    g = Gears(debug_level = options.debug_level)

    if cmd == 'list':
        # if the user doesn't give a query, grab everything
        if not args:
            g.get_torrent_info()
            torrents = g.torrents
        else:
            try:
                torrents = g.parse_query(args)
            except g.QueryException, e:
                parser.error(str(e))

        expression_re = re.compile(r'@\{([^}]+)\}')

        lines = []
        for t in torrents:
            # generate output from torrent info dict
            try: 
                s = options.output_format % t
            except KeyError:
                parser.error("invalid output format")

            # evaluate any expressions in the output (@{...})
            for m in expression_re.finditer(s):
                repl = str(eval(m.group(1)))
                s = expression_re.sub(repl, s, 1)

            lines.append(s)

        if lines:
            print options.record_separator.join(lines)

    elif cmd == 'remove' or cmd == 'start' or cmd == 'stop' or cmd == 'verify':
        if not args:
            parser.error("invalid query")

        try:
            torrents = g.parse_query(args)
        except g.QueryException, e:
            parser.error(str(e))

        if len(torrents) == 0:
            # FIXME
            raise Exception("No matching torrents")

        # method mappings
        d = dict(
            remove = g.remove_torrents,
            start = g.start_torrents,
            stop = g.stop_torrents,
            verify = g.verify_torrents,
        )

        if options.debug_level:
            s = '; '.join([ t['name'] for t in torrents])
            print "%s: %s" % (cmd, s)

        if not options.dry_run:
            d[cmd](torrents)

    elif cmd == 'add':
        if not args:
            parser.error("invalid files to add")

        for f in args:
            if not os.path.exists(f):
                parser.error("torrent '%s' does not exist" % f)
        
            f = os.path.abspath(f)
            
            if options.debug_level:
                print "adding: %s" % f

            if not options.dry_run:
                g.add_torrent(f)

    else: 
        parser.error("invalid command")
