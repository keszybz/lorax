#
# ltmpl.py
#
# Copyright (C) 2009  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Red Hat Author(s):  Martin Gracik <mgracik@redhat.com>
#                     Will Woods <wwoods@redhat.com>
#

import logging
logger = logging.getLogger("pylorax.ltmpl")

import os, re, glob, shlex, fnmatch
from os.path import basename, isdir
from subprocess import check_call

from sysutils import joinpaths, cpfile, mvfile, replace, remove
from yumhelper import * # Lorax*Callback classes
from base import DataHolder

from mako.lookup import TemplateLookup
from mako.exceptions import text_error_template
import sys, traceback

class LoraxTemplate(object):
    def __init__(self, directories=["/usr/share/lorax"]):
        # we have to add ["/"] to the template lookup directories or the
        # file includes won't work properly for absolute paths
        self.directories = ["/"] + directories

    def parse(self, template_file, variables):
        lookup = TemplateLookup(directories=self.directories)
        template = lookup.get_template(template_file)

        try:
            textbuf = template.render(**variables)
        except:
            logger.error(text_error_template().render())
            raise

        # split, strip and remove empty lines
        lines = textbuf.splitlines()
        lines = map(lambda line: line.strip(), lines)
        lines = filter(lambda line: line, lines)

        # remove comments
        lines = filter(lambda line: not line.startswith("#"), lines)

        # mako template now returns unicode strings
        lines = map(lambda line: line.encode("utf8"), lines)

        # split with shlex and perform brace expansion
        lines = map(split_and_expand, lines)

        self.lines = lines
        return lines

def split_and_expand(line):
    return [exp for word in shlex.split(line) for exp in brace_expand(word)]

def brace_expand(s):
    if not ('{' in s and ',' in s and '}' in s):
        yield s
    else:
        right = s.find('}')
        left = s[:right].rfind('{')
        (prefix, choices, suffix) = (s[:left], s[left+1:right], s[right+1:])
        for choice in choices.split(','):
            for alt in brace_expand(prefix+choice+suffix):
                yield alt

def rglob(pathname, root="/", fatal=False):
    seen = set()
    rootlen = len(root)+1
    for f in glob.iglob(joinpaths(root, pathname)):
        if f not in seen:
            seen.add(f)
            yield f[rootlen:] # remove the root to produce relative path
    if fatal and not seen:
        raise IOError, "nothing matching %s in %s" % (pathname, root)

def rexists(pathname, root=""):
    return True if rglob(pathname, root) else False

# XXX NOTE: symlinks to stuff outside inroot/outroot will make us operate
# on files outside our roots (e.g. deleting files on the host system).
# TODO: operate inside an actual chroot for safety? Not that RPM bothers..
class LoraxTemplateRunner(object):
    def __init__(self, inroot, outroot, yum=None, fatalerrors=True,
                                        templatedir=None, defaults={}):
        self.inroot = inroot
        self.outroot = outroot
        self.yum = yum
        self.fatalerrors = fatalerrors
        self.templatedir = templatedir or "/usr/share/lorax"
        # some builtin methods
        self.builtins = DataHolder(exists=lambda p: rexists(p, root=inroot),
                                   glob=lambda g: list(rglob(g, root=inroot)))
        self.defaults = defaults
        self.results = DataHolder(treeinfo=dict()) # just treeinfo for now
        # TODO: set up custom logger with a filter to add line info

    def _out(self, path):
        return joinpaths(self.outroot, path)
    def _in(self, path):
        return joinpaths(self.inroot, path)

    def _filelist(self, *pkgs):
        pkglist = self.yum.doPackageLists(pkgnarrow="installed", patterns=pkgs)
        return set([f for pkg in pkglist.installed for f in pkg.filelist])

    def _getsize(self, *files):
        return sum(os.path.getsize(self._out(f)) for f in files if os.path.isfile(self._out(f)))

    def run(self, templatefile, **variables):
        for k,v in self.defaults.items() + self.builtins.items():
            variables.setdefault(k,v)
        logger.debug("parsing %s", templatefile)
        self.templatefile = templatefile
        t = LoraxTemplate(directories=[self.templatedir])
        commands = t.parse(templatefile, variables)
        self._run(commands)


    def _run(self, parsed_template):
        logger.info("running %s", self.templatefile)
        for (num, line) in enumerate(parsed_template,1):
            logger.debug("template line %i: %s", num, " ".join(line))
            skiperror = False
            (cmd, args) = (line[0], line[1:])
            # Following Makefile convention, if the command is prefixed with
            # a dash ('-'), we'll ignore any errors on that line.
            if cmd.startswith('-'):
                cmd = cmd[1:]
                skiperror = True
            try:
                # grab the method named in cmd and pass it the given arguments
                f = getattr(self, cmd, None)
                if cmd[0] == '_' or cmd == 'run' or not callable(f):
                    raise ValueError, "unknown command %s" % cmd
                f(*args)
            except Exception:
                if skiperror:
                    logger.error("template command error in %s (ignored):", self.templatefile)
                    logger.error("  %s", " ".join(line))
                    continue
                logger.error("template command error in %s:", self.templatefile)
                logger.error("  %s", " ".join(line))
                # format the exception traceback
                exclines = traceback.format_exception(*sys.exc_info())
                # skip the bit about "ltmpl.py, in _run()" - we know that
                exclines.pop(1)
                # log the "ErrorType: this is what happened" line
                logger.error("  " + exclines[-1].strip())
                # and log the entire traceback to the debug log
                for line in ''.join(exclines).splitlines():
                    logger.debug("  " + line)
                if self.fatalerrors:
                    raise

    def install(self, srcglob, dest):
        '''
        install SRC DEST
          Copy the given file (or files, if a glob is used) from the input
          tree to the given destination in the output tree.
          The path to DEST must exist in the output tree.
          If DEST is a directory, SRC will be copied into that directory.
          If DEST doesn't exist, SRC will be copied to a file with that name,
          assuming the rest of the path exists.
          This is pretty much like how the 'cp' command works.
          Examples:
            install usr/share/myconfig/grub.conf /boot
            install /usr/share/myconfig/grub.conf.in /boot/grub.conf
        '''
        for src in rglob(self._in(srcglob), fatal=True):
            cpfile(src, self._out(dest))

    def mkdir(self, *dirs):
        '''
        mkdir DIR [DIR ...]
          Create the named DIR(s). Will create leading directories as needed.
          Example:
            mkdir /images
        '''
        for d in dirs:
            d = self._out(d)
            if not isdir(d):
                os.makedirs(d)

    def replace(self, pat, repl, *fileglobs):
        '''
        replace PATTERN REPLACEMENT FILEGLOB [FILEGLOB ...]
          Find-and-replace the given PATTERN (Python-style regex) with the given
          REPLACEMENT string for each of the files listed.
          Example:
            replace @VERSION@ ${product.version} /boot/grub.conf /boot/isolinux.cfg
        '''
        match = False
        for g in fileglobs:
            for f in rglob(self._out(g)):
                match = True
                replace(f, pat, repl)
        if not match:
            raise IOError, "no files matched %s" % " ".join(fileglobs)

    def append(self, filename, data):
        '''
        append FILE STRING
          Append STRING (followed by a newline character) to FILE.
          Python character escape sequences ('\\n', '\\t', etc.) will be
          converted to the appropriate characters.
          Examples:
            append /etc/depmod.d/dd.conf "search updates built-in"
            append /etc/resolv.conf ""
        '''
        with open(self._out(filename), "a") as fobj:
            fobj.write(data.decode('string_escape')+"\n")

    def treeinfo(self, section, key, *valuetoks):
        '''
        treeinfo SECTION KEY ARG [ARG ...]
          Add an item to the treeinfo data store.
          The given SECTION will have a new item added where
          KEY = ARG ARG ...
          Example:
            treeinfo images-${kernel.arch} boot.iso images/boot.iso
        '''
        if section not in self.results.treeinfo:
            self.results.treeinfo[section] = dict()
        self.results.treeinfo[section][key] = " ".join(valuetoks)

    def installkernel(self, section, src, dest):
        '''
        installkernel SECTION SRC DEST
          Install the kernel from SRC in the input tree to DEST in the output
          tree, and then add an item to the treeinfo data store, in the named
          SECTION, where "kernel" = DEST.

          Equivalent to:
            install SRC DEST
            treeinfo SECTION kernel DEST
        '''
        self.install(src, dest)
        self.treeinfo(section, "kernel", dest)

    def installinitrd(self, section, src, dest):
        '''
        installinitrd SECTION SRC DEST
          Same as installkernel, but for "initrd".
        '''
        self.install(src, dest)
        self.treeinfo(section, "initrd", dest)

    def hardlink(self, src, dest):
        '''
        hardlink SRC DEST
          Create a hardlink at DEST which is linked to SRC.
        '''
        if isdir(self._out(dest)):
            dest = joinpaths(dest, basename(src))
        os.link(self._out(src), self._out(dest))

    def symlink(self, target, dest):
        '''
        symlink SRC DEST
          Create a symlink at DEST which points to SRC.
        '''
        if rexists(self._out(dest)):
            self.remove(dest)
        os.symlink(target, self._out(dest))

    def copy(self, src, dest):
        '''
        copy SRC DEST
          Copy SRC to DEST.
          If DEST is a directory, SRC will be copied inside it.
          If DEST doesn't exist, SRC will be copied to a file with
          that name, if the path leading to it exists.
        '''
        cpfile(self._out(src), self._out(dest))

    def move(self, src, dest):
        '''
        move SRC DEST
          Move SRC to DEST.
        '''
        mvfile(self._out(src), self._out(dest))

    def remove(self, *fileglobs):
        '''
        remove FILEGLOB [FILEGLOB ...]
          Remove all the named files or directories.
          Will *not* raise exceptions if the file(s) are not found.
        '''
        for g in fileglobs:
            for f in rglob(self._out(g)):
                remove(f)

    def chmod(self, fileglob, mode):
        '''
        chmod FILEGLOB OCTALMODE
          Change the mode of all the files matching FILEGLOB to OCTALMODE.
        '''
        for f in rglob(self._out(fileglob), fatal=True):
            os.chmod(f, int(mode,8))

    # TODO: do we need a new command for gsettings?
    def gconfset(self, path, keytype, value, outfile=None):
        '''
        gconfset PATH KEYTYPE VALUE [OUTFILE]
          Set the given gconf PATH, with type KEYTYPE, to the given value.
          OUTFILE defaults to /etc/gconf/gconf.xml.defaults if not given.
          Example:
            gconfset /apps/metacity/general/num_workspaces int 1
        '''
        if outfile is None:
            outfile = self._out("etc/gconf/gconf.xml.defaults")
        check_call(["gconftool-2", "--direct",
                    "--config-source=xml:readwrite:%s" % outfile,
                    "--set", "--type", keytype, path, value])

    def log(self, msg):
        '''
        log MESSAGE
          Emit the given log message. Be sure to put it in quotes!
          Example:
            log "Reticulating splines, please wait..."
        '''
        logger.info(msg)

    # TODO: add ssh-keygen, mkisofs(?), find, and other useful commands
    def runcmd(self, *cmdlist):
        '''
        runcmd CMD [--chdir=DIR] [ARG ...]
          Run the given command with the given arguments.
          If "--chdir=DIR" is given, change to the named directory
          before executing the command.

          NOTE: All paths given MUST be COMPLETE, ABSOLUTE PATHS to the file
          or files mentioned. ${root}/${inroot}/${outroot} are good for
          constructing these paths.

          FURTHER NOTE: Please use this command only as a last resort!
          Whenever possible, you should use the existing template commands.
          If the existing commands don't do what you need, fix them!

          Examples:
            (this should be replaced with a "find" function)
            runcmd find ${root} -name "*.pyo" -type f -delete
            %for f in find(root, name="*.pyo"):
                remove ${f}
            %endfor
        '''
        chdir = lambda: None
        cmd = cmdlist
        if cmd[0].startswith("--chdir="):
            dirname = cmd[0].split('=',1)[1]
            chdir = lambda: os.chdir(dirname)
            cmd = cmd[1:]
        check_call(cmd, preexec_fn=chdir)

    def installpkg(self, *pkgs):
        '''
        installpkg PKGGLOB [PKGGLOB ...]
          Request installation of all packages matching the given globs.
          Note that this is just a *request* - nothing is *actually* installed
          until the 'run_pkg_transaction' command is given.
        '''
        for p in pkgs:
            try:
                self.yum.install(pattern=p)
            except Exception as e:
                # FIXME: save exception and re-raise after the loop finishes
                logger.warn("installpkg %s failed: %s",p,str(e))

    def removepkg(self, *pkgs):
        '''
        removepkg PKGGLOB [PKGGLOB...]
          Delete the named package(s).
          IMPLEMENTATION NOTES:
            RPM scriptlets (%preun/%postun) are *not* run.
            Files are deleted, but directories are left behind.
        '''
        for p in pkgs:
            filepaths = [f.lstrip('/') for f in self._filelist(p)]
            # TODO: also remove directories that aren't owned by anything else
            if filepaths:
                logger.debug("removepkg %s: %ikb", p, self._getsize(*filepaths)/1024)
                self.remove(*filepaths)
            else:
                logger.debug("removepkg %s: no files to remove!", p)

    def run_pkg_transaction(self):
        '''
        run_pkg_transaction
          Actually install all the packages requested by previous 'installpkg'
          commands.
        '''
        self.yum.buildTransaction()
        self.yum.repos.setProgressBar(LoraxDownloadCallback())
        self.yum.processTransaction(callback=LoraxTransactionCallback(),
                                    rpmDisplay=LoraxRpmCallback())
        self.yum.closeRpmDB()

    def removefrom(self, pkg, *globs):
        '''
        removefrom PKGGLOB [--allbut] FILEGLOB [FILEGLOB...]
          Remove all files matching the given file globs from the package
          (or packages) named.
          If '--allbut' is used, all the files from the given package(s) will
          be removed *except* the ones which match the file globs.
          Examples:
            removefrom usbutils /usr/bin/*
            removefrom xfsprogs --allbut /sbin/*
        '''
        cmd = "%s %s" % (pkg, " ".join(globs)) # save for later logging
        keepmatches = False
        if globs[0] == '--allbut':
            keepmatches = True
            globs = globs[1:]
        # get pkg filelist and find files that match the globs
        filelist = self._filelist(pkg)
        matches = set()
        for g in globs:
            globs_re = re.compile(fnmatch.translate(g))
            m = filter(globs_re.match, filelist)
            if m:
                matches.update(m)
            else:
                logger.debug("removefrom %s %s: no files matched!", pkg, g)
        # are we removing the matches, or keeping only the matches?
        if keepmatches:
            remove = filelist.difference(matches)
        else:
            remove = matches
        # remove the files
        if remove:
            logger.debug("%s: removed %i/%i files, %ikb/%ikb", cmd,
                             len(remove), len(filelist),
                             self._getsize(*remove)/1024, self._getsize(*filelist)/1024)
            self.remove(*remove)
        else:
            logger.debug("%s: no files to remove!", cmd)
