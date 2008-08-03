#!/usr/bin/python

import fnmatch
import os
import re
import socket
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

            # merge the status and info dictionaries together to create the
            # definitive torrent info dict
            t.update(status[i])
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
            if not m:
                continue
            else:
                key, operator, value = m.groups()

                if operator == '=':
                    # if there are globbing metacharaters, glob-match
                    if re.search('[*?[]', value):
                        f = lambda v: fnmatch.fnmatch(v, value) 
                    # otherwise, perform a strict equality match
                    else:
                        f = lambda v: v == value
                elif operator == '~':
                    f = lambda v: re.search(value, v)
                elif operator == '>':
                    try:
                        value = float(value)
                    except ValueError:
                        parser.error("invalid filter value")

                    f = lambda v: float(v) > value
                elif operator == '<':
                    try:
                        value = float(value)
                    except ValueError:
                        parser.error("invalid filter value")

                    f = lambda v: float(v) < value

                filters[key] = f

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

            try: 
                print options.output_format % t
            except KeyError:
                parser.error("invalid output format")
