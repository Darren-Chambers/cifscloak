#!/usr/bin/env python3

import os
import sys
import stat
import time
import json
import regex
import sqlite3
import argparse
import subprocess
from syslog import syslog
from getpass import getpass
from cryptography.fernet import Fernet

class Cifscloak():

    cifstabschema = '''
        CREATE TABLE IF NOT EXISTS cifstab(
        name,
        address,
        sharename,
        mountpoint,
        options,
        user,
        password,
        PRIMARY KEY (name)
        )
        '''

    retryschema = {
        'mount': {'16':'Device or resource busy'},
        'umount': ['target is busy.']
        }

    def __init__(self,cifstabdir='/root/.cifstab',keyfile='.keyfile',cifstab='.cifstab.db',retries=3,waitsecs=5):
        self.status = { 'error':0, 'successcount':0, 'failedcount':0, 'success':[], 'failed': [], 'attempts': {}, 'messages':[] }
        self.mountprocs = {}
        self.retries = retries
        self.waitsecs = waitsecs
        self.cifstabdir = cifstabdir
        self.cifstab = self.cifstabdir+os.sep+cifstab
        self.keyfile = self.cifstabdir+os.sep+keyfile
        self.exit = 0

        try:
            if not os.path.exists(self.cifstabdir): os.makedirs(self.cifstabdir)
        except PermissionError:
            print('PermissionError - must be root user to read cifstab')
            sys.exit(1)

        os.chmod(self.cifstabdir,stat.S_IRWXU)
        self.db = sqlite3.connect(self.cifstab)
        self.cursor = self.db.cursor()
        self.cursor.execute("PRAGMA auto_vacuum = FULL")
        self.cursor.execute(self.cifstabschema)

        if not os.path.exists(self.keyfile):
            with open(self.keyfile,'wb') as f:
                f.write(Fernet.generate_key())
            os.chmod(self.keyfile,stat.S_IRUSR)
        self.key = Fernet(open(self.keyfile,'rb').read())

    def checkstatus(self):
        if self.status['error']:
            print(json.dumps(self.status,indent=4))
        parser.exit(status=cifscloak.status['error'])

    def addmount(self,args):
        password = getpass()
        try:
            self.cursor.execute('''
            INSERT INTO cifstab (name,address,sharename,mountpoint,options,user,password)
            VALUES (?,?,?,?,?,?,?)''',
            (args.name,self.encrypt(args.ipaddress),self.encrypt(args.sharename),self.encrypt(args.mountpoint),self.encrypt(args.options),self.encrypt(args.user),self.encrypt(password)))
            self.db.commit()
        except sqlite3.IntegrityError:
            print("Cifs mount name must be unique\nExisting names:")
            self.listmounts(None)

    def removemounts(self,args):
        for name in args.names:
            self.cursor.execute('''DELETE FROM cifstab WHERE name = ?''',(name,))
            self.db.commit()
    
    def listmounts(self,args):
        mounts = []
        self.cursor.execute('''SELECT name FROM cifstab''')
        for r in self.cursor:
            mounts.append(r[0])
            print(r[0])
        return mounts

    def mount(self,args):

        if args.all:
            mounts = self.listmounts(None)
        else:
            mounts = list(dict.fromkeys(args.names))

        for name in mounts:
            cifsmount = self.getcredentials(name)
            if not len(cifsmount):
                message = "cifs name {} not found in cifstab".format(name)
                syslog(message)
                print(message)
                self.status['messages'].append(message)
                self.status['error'] = 1
                continue
            if args.u:
                syslog("Attempting umount {}".format(name))
                cifscmd = "umount {}".format(cifsmount['mountpoint'])
                retryon = list(self.retryschema['umount'])
            else:
                syslog("Attempting mount {}".format(name))
                if not os.path.exists(cifsmount['mountpoint']):
                    os.makedirs(cifsmount['mountpoint'])
                cifscmd = "mount -t cifs -o username={},password={},{} //{}/{} {}".format(cifsmount['user'],cifsmount['password'],cifsmount['options'],cifsmount['address'],cifsmount['sharename'],cifsmount['mountpoint'])
                retryon = list(self.retryschema['mount'])
            self.execute(cifscmd,name,retryon)

    def encrypt(self,plain):
        return self.key.encrypt(bytes(plain,encoding='utf-8'))

    def decrypt(self,encrypted):
        return self.key.decrypt(encrypted).decode('utf-8')

    def getcredentials(self,name):
        credentials = {}
        self.cursor.execute('''SELECT name,address,sharename,mountpoint,options,user,password from cifstab WHERE name = ?''',(name,))
        for r in self.cursor:
            credentials = { 'name':r[0], 'address':self.decrypt(r[1]), 'sharename':self.decrypt(r[2]), 'mountpoint':self.decrypt(r[3]), 'options':self.decrypt(r[4]), 'user':self.decrypt(r[5]), 'password':self.decrypt(r[6]) }
        return credentials

    def execute(self,cmd,name,retryon=[],expectedreturn=0):
        returncode = None
        self.status['attempts'][name] = 0
        while returncode != expectedreturn and self.status['attempts'][name] < self.retries:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
            stdout, stderr = proc.communicate()
            returncode = proc.returncode
            if proc.returncode and proc.returncode != expectedreturn:
                mounterr = regex.search(r'(?|error\((\d+)\)|.+:.+:\s{1}(.+))',stderr).group(1)
                syslog('Error: {}'.format(stderr))
                syslog('Returned: {}'.format(proc.returncode))
                syslog('MountErr: {}'.format(mounterr))
                self.status['error'] = 1
                self.status['messages'].append('{}: {}'.format(name,stderr))
                sys.stderr.write(stderr)
                if str(mounterr) in retryon:
                    time.sleep(self.waitsecs)
                else:
                    message = 'mounterr {} not in retryschema, no retry attempt will be made'.format(mounterr)
                    syslog(message)
                    break

            self.status['attempts'][name] += 1
                
        if returncode != expectedreturn:
            self.status['error'] = 1
            self.status['failed'].append(name)
            self.status['failedcount'] += 1
        else:
            self.status['successcount'] += 1
            self.status['success'].append(name)


if __name__ == "__main__":

    defaultRetries = 3
    defaultWaitSecs = 5
    
    parser = argparse.ArgumentParser(description='cifscloak - command line utility for mounting cifs shares using encrypted passwords')
    subparsers = parser.add_subparsers(help='Subcommands', dest='subcommand', required=True)   
    parser_addmount = subparsers.add_parser('addmount', help="Add a cifs mount to encrypted cifstab. addmount -h for help")
    parser_addmount.add_argument("-n", "--name", help="Connection name e.g identifying server name", required=True)
    parser_addmount.add_argument("-s", "--sharename", help="Share name", required=True)
    parser_addmount.add_argument("-i", "--ipaddress", help="Server address or ipaddress", required=True)
    parser_addmount.add_argument("-m", "--mountpoint", help="Mount point", required=True)
    parser_addmount.add_argument("-u", "--user", help="User name", required=True)
    parser_addmount.add_argument("-o", "--options", help="Quoted csv options e.g. \"domain=mydomain,ro\"", default=' ', required=False)
    parser_mount = subparsers.add_parser('mount', help="Mount cifs shares, mount -h for help")
    parser_mount.add_argument("-u", action='store_true', help="Unmount the named cifs shares, e.g -a films music", required=False )
    parser_mount.add_argument("-r", "--retries", help="Retry count, useful when systemd is in play", required=False, default=3, type=int )
    parser_mount.add_argument("-w", "--waitsecs", help="Wait time in seconds between retries", required=False, default=5, type=int )
    group = parser_mount.add_mutually_exclusive_group(required=True)
    group.add_argument("-n", "--names", nargs="+", help="Mount reference names, e.g -n films music. --names and --all are mutually exclusive", required=False)
    group.add_argument("-a", "--all", action='store_true', help="Mount everything in the cifstab.", required=False)
    parser_removemounts = subparsers.add_parser('removemounts', help="Remove cifs mounts from encrypted cifstab. removemount -h for help")
    parser_removemounts.add_argument("-n", "--names", nargs="+", help="Remove cifs mounts e.g. -a films music", required=True)
    parser_listmounts = subparsers.add_parser('listmounts', help="List cifs mounts in encrypted cifstab")
    args = parser.parse_args()
    cifscloak = Cifscloak(retries=getattr(args,'retries',defaultRetries),waitsecs=getattr(args,'waitsecs',defaultWaitSecs))
    getattr(cifscloak, args.subcommand)(args)
    cifscloak.checkstatus()

