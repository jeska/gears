#!/usr/bin/python

import fnmatch
import os
import re
import socket
from decimal import Decimal
from optparse import OptionParser

from bencode import bdecode, bencode

socket_path = "%s/.transmission/daemon/socket" % os.environ['HOME']

class Gears:
    class QueryException(Exception):
        pass

    def __init__(self):
        self.connect()

    def connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(socket_path)

        self.socket = s

        # send protocol handshake
        handshake = dict(version = dict(label = 'gears', max = 2, min = 1))
        self.send_message(handshake)

    def send_message(self, message, **kw):
        socket = self.socket

        read = kw.get('read', True)

        cmd = bencode(message)

        # the first eight bytes of a message is the length of the entire message
        # in hex zero-padded to fit the whole eight bytes
        length = str(hex(len(cmd)))[2:].zfill(8)
        message = length + cmd

        socket.send(message)

        if read:
            return self.read_message()
        else:
            return

    def read_message(self):
        socket = self.socket

        # the first eight bytes is the length of the message
        length = int(socket.recv(8), 16)
        data = socket.recv(length)
        return bdecode(data)

    def get_torrent_info(self):
        # if self.torrents already exists, assume it's populated and return
        try:
            self.torrents
        except AttributeError:
            pass
        else:
            return

        socket = self.socket

        info = self.send_message(['get-info-all', ['hash', 'name', 'size']])[1]

        status = self.send_message(['get-status-all', ['completed',
            'download-speed', 'download-total', 'error', 'error-message', 'eta',
            'state', 'upload-speed', 'upload-total']])[1]

        torrents = []
        i = 0
        for t in info:
            # use internal transmission id as the dictionary key
            k = t['id']

            # merge the status and info dictionaries together to lump all the
            # torrent's details together
            t.update(status[i])

            # add some useful keys
            ratio = Decimal(t['upload-total']) / Decimal(t['size'])
            t['ratio'] = ratio.quantize(Decimal('0.01'))

            # add the torrent 
            torrents.append(t)

            i += 1

        self.torrents = torrents

        return

    def remove_torrent(self, t):
        return self.send_message(['remove', [t['id']]], read = False)

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
            (?<!\\)           # don't match if preceeded with a backslash
            /                 # flags follow a forward slash
            ([ilmsuxILMSUX]+) # group the flags
            \Z                # end of string
        '''
        flags_re = re.compile(flags_re, re.X)

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
                        # get the actual re.[ilmsux] value for the flag
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
    parser = OptionParser(usage="usage: %prog [options] command [args]")
    parser.add_option("-0", action="store_const", const="\0", dest="record_separator")
    parser.add_option("-H", "--hashes", action="store_const", const="%hash", dest="output_format")
    parser.add_option("-o", "--output_format", dest="output_format", default="%name")
    parser.add_option("--rs", dest="record_separator", default="\n")

    (options, args) = parser.parse_args()

    # make the output format into a printf-friendly string
    options.output_format = re.sub(r'%([A-Za-z-]+)', r'%(\1)s', options.output_format)

    try:
        cmd = args[0]
        query = args[1:]
    except IndexError:
        parser.error("incorrect number of arguments")

    g = Gears()

    if cmd == 'list':
        # if the user doesn't give a query, grab everything
        if not query:
            g.get_torrent_info()
            torrents = g.torrents
        else:
            try:
                torrents = g.parse_query(query)
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

    elif cmd == 'remove':
        if not query:
            parser.error("invalid query")

        try:
            torrents = g.parse_query(args[1:])
        except g.QueryException, e:
            parser.error(str(e))

        for t in torrents:
            g.remove_torrent(t)

    else: 
        parser.error("invalid command")
