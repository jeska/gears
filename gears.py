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
            return True

    def read_message(self):
        socket = self.socket

        # the first eight bytes is the length of the message
        length = int(socket.recv(8), 16)
        data = socket.recv(length)
        return bdecode(data)

    def get_torrent_info(self):
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

        torrents = {}
        id_of = {}
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

            # add the torrent to the dictionary
            torrents[k] = t

            # reverse mapping
            id_of[t['name']] = k

            i += 1

        self.torrents = torrents
        self.id_of = id_of

class FilterFalseException(Exception):
    pass

if __name__ == '__main__':
    parser = OptionParser(usage="usage: %prog [options] command")
    parser.add_option("-H", "--hashes", action="store_const", const="%hash", dest="output_format")
    parser.add_option("-o", "--output_format", dest="output_format", default="%name")

    (options, args) = parser.parse_args()

    # make the output format into a printf-friendly string
    options.output_format = re.sub(r'%([A-Za-z-]+)', r'%(\1)s', options.output_format)

    try:
        cmd = args[0]
    except IndexError:
        parser.error("incorrect number of arguments")

    g = Gears()

    if cmd == 'list':
        g.get_torrent_info()

        filters = {}
        for arg in args[1:]:
            regex = r'''
                ([A-Za-z-]+) # key
                ([=~<>])     # operator
                             #     = equality (if "*" and "?" are in the
                             #         value, switch to globbing mode)
                             #     ~ regex
                             #     < less than
                             #     > greater than
                ([^ ]+)      # value
            '''

            m = re.compile(regex, re.X).search(arg)

            # match the name by default
            while not m:
                arg = "name=%s" % arg
                m = re.compile(regex, re.X).search(arg)

            key, operator, value = m.groups()

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
                f = lambda v, value=value: re.search(value, v)
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

#            if negation:
#                f = lambda v: not f(v)

            filters[key] = f

        expression_re = re.compile(r'@\{([^}]+)\}')

        for t in g.torrents.itervalues():
            try:
                for k, filter in filters.iteritems(): 
                    try:
                        if not filter(t[k]):
                            raise FilterFalseException
                    except KeyError:
                        parser.error("invalid filter key")
            except FilterFalseException:
                continue

            # generate output from torrent info dict
            try: 
                s = options.output_format % t
            except KeyError:
                parser.error("invalid output format")

            # evaluate any expressions in the output (@{...})
            for m in expression_re.finditer(s):
                repl = str(eval(m.group(1)))
                s = expression_re.sub(repl, s, 1)

            print s
