#!/usr/bin/env python

"""bump mozbase versions"""

import optparse
import os
import pkg_resources
import re
import subprocess
import sys
import xmlrpclib

# import setup_development.py from the same directory
import setup_development

here = setup_development.here
REPOSITORY_URL = 'git@github.com:mozilla/mozbase.git'
REPOSITORY_PULL_URL = 'git://github.com/mozilla/mozbase.git'

class CalledProcessError(Exception):
    """error for bad calls"""


class VersionBump(object):
    """ Main versionbump class """

    class depends(object):
        """Dependencies decorator"""
        def __init__(self, *deps):
            """ takes in dependencies as string parameters """
            self.deps = deps

        def __call__(self, func):
            """makes sure that the dependencies are met before running func
            !! if a dependency has already been met, that function will not be
            run again"""
            def wrapped(vb_self):
                for func_name in self.deps:
                    if func_name not in vb_self.done:
                        f = getattr(vb_self, func_name)
                        f()
                        vb_self.done.append(func_name)
                func(vb_self)
            return wrapped

    def __init__(self, args, commit_message, diff, dry_run, git_path, info,
                 pypi_versions, strict, tag_only):
        self.args = args
        self.commit_message = commit_message
        self.dependencies = None
        self.dependent_versions = None
        self.diff = diff
        self.directories = None
        self.done = []
        self.dry_run = dry_run
        self.git_path = git_path
        self.info = info
        self.pypi_versions = pypi_versions
        self.strict = strict
        self.tag_only = tag_only
        self.to_do = None
        self.versions = None

    def fail(self, msg=''):
        print >> sys.stderr, "FAILED: %s" % msg
        sys.exit(1)

    def format_version(self, **dep):
        """formats a dependency version"""
        return '%(Name)s %(Type)s %(Version)s' % dep

    def call(self, cmd, **kwargs):
        print "Running %s, %s" % (cmd, kwargs)

        if self.dry_run:
            return
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        process = subprocess.Popen(cmd, **kwargs)
        stdout, stderr = process.communicate()
        if process.returncode:
            print 'stdout:'
            print stdout
            print 'stderr:'
            print stderr
            raise CalledProcessError("Error running %s: %d" %
                                     (cmd, process.returncode))

    def revert(self, git):
        """revert the repository on error"""
        self.call([git, 'reset', '--hard', 'HEAD'])

    @depends('gather_info')
    def print_pkgs_info(self):
        """print package version information"""
        for value in self.info.values():
            print '%s %s : %s' % (value['Name'], value['Version'],
                                  ', '.join(self.dependencies[value['Name']]))

    def gather_info(self):
        """get package information"""
        self.info = {}
        self.dependencies = {}
        self.directories = {}
        for package in setup_development.mozbase_packages:
            directory = os.path.join(here, package)
            self.info[directory] = setup_development.info(directory)
            name, _dependencies = setup_development.get_dependencies(directory)
            assert name == self.info[directory]['Name']
            self.directories[name] = directory
            self.dependencies[name] = _dependencies

    @depends('gather_info')
    def print_pypi_versions(self):
        """print package version information and
        version info from pypi and exit"""
        client = xmlrpclib.ServerProxy('http://pypi.python.org/pypi')
        for value in self.info.values():
            versions = client.package_releases(value['Name'])
            if versions:
                version = max(versions,
                              key=lambda x: pkg_resources.parse_version(x))
            else:
                version = None
            print '%s %s : pypi version %s' % (value['Name'], value['Version'],
                                               version)

    def check_pypirc(self):
        """makes sure there is a pypirc in $HOME"""
        home = os.environ['HOME']
        pypirc = os.path.join(home, '.pypirc')
        print "Checking for pypirc: %s" % pypirc
        if not os.path.exists(pypirc):
            self.fail("%s not found." % pypirc)

    def check_on_master(self):
        """makes sure we are on the master branch"""
        cmd = [self.git_path, 'branch']
        process = subprocess.Popen(cmd,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=here)
        stdout, stderr = process.communicate()
        if stderr or process.returncode:
            print 'stdout:'
            print stdout
            print 'stderr:'
            print stderr
            raise CalledProcessError("Error running %s: %d" % (cmd,
                                                               process.returncode))
        branch = [line for line in stdout.splitlines() if line.startswith('*')][0]
        branch = branch.split('*', 1)[-1].strip()
        if branch != 'master':
            self.fail("versionbump.py must be used on the master branch")

    def check_clean_dir(self):
        cmd = [self.git_path, 'status', '-s']
        process = subprocess.Popen(cmd,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=here)
        stdout, stderr = process.communicate()
        if stderr or process.returncode:
            raise CalledProcessError("Error running %s: %d" % (cmd,
                                                               process.returncode))
        if stdout.strip():
            self.fail("%s directory unclean when running git status -s" % here)

    def parse_arg_list(self):
        """finds the new versions"""
        self.versions = {}
        msg = "Versions should be of the form 'package=version' (You gave: '%s')"
        for arg in self.args:
            if arg.count('=') != 1:
                self.fail(msg % arg)
            package, version = arg.split('=')
            self.versions[package] = version

    @depends('parse_arg_list', 'gather_info')
    def check_pkgs_list(self):
        """makes sure all the specified packages exist"""
        unrecognized = [package for package in self.versions
                        if package not in self.dependencies]
        if unrecognized:
            self.fail("Not a package: %s" % ', '.join(unrecognized))

    @depends('check_pkgs_list')
    def check_dependent_pkgs(self):
        """record ancillary packages that need bumping
        and ensure that if you're bumping versions, you're
        bumping them for all packages affected"""
        self.dependent_versions = {}
        missing = []
        types = ['==']
        if not self.strict:
            types.append('>=')
        for name, deps in self.dependencies.items():
            for dep in deps:
                dep_info = setup_development.dependency_info(dep)
                if dep_info['Type'] in types and dep_info['Name'] in self.versions:
                    if name not in self.versions:
                        missing.append(name)
                    self.dependent_versions.setdefault(name, []).append(dep_info)

        if missing:
            missing = dict([('%s %s' % (i, self.info[self.directories[i]]['Version']),
                             [self.format_version(**k) for k in j])
                            for i, j in self.dependent_versions.items()
                            if i in missing])
            self.fail("Bumping %s, but you also need to bump %s" % (self.versions,
                                                                    missing))

    @depends('check_on_master', 'check_clean_dir')
    def check_up_to_date(self):
        print "Pulling from %s master" % REPOSITORY_PULL_URL
        self.call([self.git_path, 'pull', REPOSITORY_PULL_URL, 'master'],
         stdout=None, stderr=None, cwd=here)

    @depends('check_dependent_pkgs', 'check_up_to_date')
    def bump_specified_pkgs(self):
        """bump versions of desired files"""
        for name, newversion in self.versions.items():
            directory = self.directories[name]
            oldversion = self.info[directory]['Version']
            print "Bumping %s == %s => %s" % (name, oldversion, newversion)
            setup_py = os.path.join(directory, 'setup.py')
            f = file(setup_py)
            lines = f.readlines()
            f.close()
            regex_string = r"""PACKAGE_VERSION *= *['"]%s["'].*""" % re.escape(oldversion)
            regex = re.compile(regex_string)
            for index, line in enumerate(lines):
                if regex.match(line):
                    break
            else:
                self.revert(self.git_path)
                self.fail('PACKAGE_VERSION = "%s" not found in %s' % (oldversion,
                                                                      setup_py))
            if not self.dry_run:
                lines[index] = "PACKAGE_VERSION = '%s'\n" % newversion
                f = file(setup_py, 'w')
                for line in lines:
                    f.write(line)
                f.close()

    @depends('bump_specified_pkgs')
    def bump_dependent_pkgs(self):
        """bump version of dependencies"""
        for package, deps in self.dependent_versions.items():
            print "Bumping dependencies %s of %s" % ([self.format_version(**dep)
                                                      for dep in deps],
                                                     package)
            regexes = [(dep,
                        re.compile(r"%s *%s *%s" % (re.escape(dep['Name']),
                                                    re.escape(dep['Type']),
                                                    re.escape(dep['Version'])),
                                   flags=re.MULTILINE)
                        )
                       for dep in deps]
            setup_py = os.path.join(self.directories[package], 'setup.py')
            assert os.path.exists(setup_py)
            f = file(setup_py)
            contents = f.read()
            f.close()
            for dep, regex in regexes:
                newversion = '%s %s %s' % (dep['Name'], dep['Type'],
                                           self.versions[dep['Name']])
                formatted = self.format_version(**dep)
                print "- Bumping dependency %s => %s" % (formatted, newversion)
                matches = regex.findall(contents)
                if len(matches) != 1:
                    self.revert(self.git_path)
                    if not matches:
                        msg = "Could not find dependency %s in %s" % (formatted,
                                                                      setup_py)
                    else:
                        msg = "Multiple matches for %s in %s" % (formatted,
                                                                 setup_py)
                    self.fail(msg)
                if not self.dry_run:
                    contents = regex.sub(newversion, contents)
            if not self.dry_run:
                f = file(setup_py, 'w')
                f.write(contents)
                f.close()

    def write_diff(self):
        if self.dry_run:
            return
        if self.diff == '-':
            f = sys.stdout
            filename = 'stdout'
        else:
            f = file(self.diff, 'w')
            filename = self.diff
        print "Writing diff to %s" % filename
        process = subprocess.Popen([self.git_path, 'diff'],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=here)
        stdout, stderr = process.communicate()
        if process.returncode:
            print 'stdout:'
            print stdout
            print 'stderr:'
            print stderr
            self.fail("Error running `%s diff`" % self.git_path)
        f.write(stdout)
        if self.diff != '-':
            f.close()
        self.revert(self.git_path) # get back to your old state

    @depends('bump_dependent_pkgs')
    def commit(self):
        if not self.commit_message:
            self.fail("No commit --message given; not updating git or pushing to pypi")
        print "Commit changes to %s: %s" % (REPOSITORY_URL, self.commit_message)
        self.call([self.git_path, 'commit', '-a', '-m', self.commit_message], cwd=here)

    def push(self):
        self.call([self.git_path, 'push', REPOSITORY_URL, 'master'],
             stdout=None, stderr=None, cwd=here)

    @depends('parse_arg_list')
    def tag(self):
        tags = [('%s-%s' % (package, version))
                for package, version in self.versions.items()]
        print "Updating tags for %s: %s" % (REPOSITORY_URL, ', '.join(tags))
        self.call([self.git_path, 'pull', '--tags', REPOSITORY_URL, 'master'],
             stdout=None, stderr=None, cwd=here)
        for tag in tags:
            self.call([self.git_path, 'tag', tag], cwd=here)
        try:
            self.call([self.git_path, 'push', '--tags', REPOSITORY_URL, 'master'],
                 stdout=None, stderr=None, cwd=here)
        except CalledProcessError, e:
            print "Failure pushing tags."
            raise e

    @depends('check_dependent_pkgs')
    def upload_to_pypi(self):
        formatted_deps = dict([(package, set([dep['Name'] for dep in deps]))
                               for package, deps in self.dependent_versions.items()])
        for package in self.versions.keys():
            formatted_deps.setdefault(package, set())
        unrolled = setup_development.unroll_dependencies(formatted_deps)
        print "Uploading to pypi: %s" % ', '.join([('%s-%s' % (package,
                                                               self.versions[package]))
                                                   for package in unrolled])
        for package in unrolled:
            directory = self.directories[package]
            cmd = [sys.executable,
                   'setup.py',
                   'egg_info',
                   '-RDb',
                   '',
                   'sdist',
                   'upload']
            try:
                self.call(cmd, cwd=directory)
            except CalledProcessError, e:
                print """Failure uploading package %s to pypi.
    Make sure you have permission to update the package
    and that your ~/.pypirc file is correct""" % package
                raise e

    def quit(self):
        sys.exit(0)

    def build_actions_list(self):
        full_run = ['commit', 'push', 'tag', 'upload_to_pypi']

        self.to_do = []
        if self.info:
            self.to_do += ['print_pkgs_info', 'quit']
        if self.diff:
            self.to_do += ['write_diff']
        if self.pypi_versions:
            self.to_do = ['print_pypi_versions']
        if self.tag_only:
            self.to_do = ['tag', 'quit']

        if 'quit' not in self.to_do:
            self.to_do += full_run

    @depends('build_actions_list')
    def run(self):
        for action in self.to_do:
            func = getattr(self, action)
            func()


def main(args=sys.argv[1:]):
    # parse command line options
    usage = '%prog [options] packageA=0.1.2 <packageB=1.2> <...>'
    parser = optparse.OptionParser(usage=usage, description=__doc__)
    parser.add_option('--info', dest='info',
                      action='store_true', default=False,
                      help="display package version information and exit")
    parser.add_option('--dry-run', dest='dry_run',
                      action='store_true', default=False,
                      help="don't make changes, just display what will be run")
    parser.add_option('--diff', dest='diff',
                      help="output the diff to this file ('-' for stdout)")
    parser.add_option('-m', '--message', dest='commit_message',
                      help="commit message")
    parser.add_option('--strict', dest='strict',
                      action='store_true', default=False,
                      help="bump dependencies specified as '==' but not '>='")
    parser.add_option('--git', dest='git_path', default='git',
                      help='git binary to use')
    parser.add_option('--pypi', dest='pypi_versions',
                      action='store_true', default=False,
                      help="display in-tree package versions and versions on pypi")
    parser.add_option('--tag-only', dest='tag_only',
                      action='store_true', default=False,
                      help="only tag the version(s) and quit")
    options, args = parser.parse_args()
    data = vars(options)
    data['args'] = args

    vb = VersionBump(**data)
    vb.run()


if __name__ == '__main__':
    main()
