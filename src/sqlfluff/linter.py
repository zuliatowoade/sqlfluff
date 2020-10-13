"""Defines the linter class."""

import os
from pathlib import Path
import time
from collections import namedtuple
import logging

# Attempt to use the C version for a speedup on comparisons
# if it's present. If not just use the normal one.
try:
    from cdifflib import CSequenceMatcher as SequenceMatcher
except ImportError:
    from difflib import SequenceMatcher

from benchit import BenchIt
import pathspec

from .errors import SQLLexError, SQLParseError
from .parser import FileSegment, RootParseContext
from .rules import get_ruleset


# Instantiate the linter logger
linter_logger = logging.getLogger('sqlfluff.linter')


class LintedFile(namedtuple('ProtoFile', ['path', 'violations', 'time_dict', 'tree', 'file_mask', 'ignore_mask'])):
    """A class to store the idea of a linted file."""
    __slots__ = ()

    def check_tuples(self):
        """Make a list of check_tuples.

        This assumes that all the violations found are
        linting violations (and therefore implement `check_tuple()`).
        If they don't then this function raises that error.
        """
        vs = []
        for v in self.get_violations():
            if hasattr(v, 'check_tuple'):
                vs.append(v.check_tuple())
            else:
                raise v
        return vs

    def get_violations(self, rules=None, types=None, filter_ignore=True, fixable=None):
        """Get a list of violations, respecting filters and ignore options.

        Optionally now with filters.
        """
        violations = self.violations
        # Filter types
        if types:
            try:
                types = tuple(types)
            except TypeError:
                types = (types,)
            violations = [v for v in violations if isinstance(v, types)]
        # Filter rules
        if rules:
            if isinstance(rules, str):
                rules = (rules,)
            else:
                rules = tuple(rules)
            violations = [v for v in violations if v.rule_code() in rules]
        # Filter fixable
        if fixable is not None:
            # Assume that fixable is true or false if not None
            violations = [v for v in violations if v.fixable is fixable]
        # Filter ignorable violations
        if filter_ignore:
            violations = [v for v in violations if not v.ignore]
            # Ignore any rules in the ignore mask
            if self.ignore_mask:
                for line_no, rules in self.ignore_mask:
                    violations = [
                        v for v in violations
                        if not (
                            v.line_no() == line_no
                            and (rules is None or v.rule_code() in rules)
                        )
                    ]
        return violations

    def num_violations(self, **kwargs):
        """Count the number of violations.

        Optionally now with filters.
        """
        violations = self.get_violations(**kwargs)
        return len(violations)

    def is_clean(self):
        """Return True if there are no ignorable violations."""
        return not any(self.get_violations(filter_ignore=True))

    def fix_string(self):
        """Obtain the changes to a path as a string.

        We use the file_mask to do a safe merge, avoiding any templated
        sections. First we need to detect where there have been changes
        between the fixed and templated versions. The file mask is of
        the format: (raw_file, templated_file, fixed_file).

        We use difflib.SequenceMatcher.get_opcodes
        See: https://docs.python.org/3.7/library/difflib.html#difflib.SequenceMatcher.get_opcodes
        It returns a list of tuples ('equal|replace|delete|insert', ia1, ia2, ib1, ib2).

        """
        bencher = BenchIt()
        bencher("fix_string: start")

        # Do we have enough information to actually fix the file?
        if any(elem is None for elem in self.file_mask):
            linter_logger.warning("Insufficient information to fix file: %s", self.file_mask)
            return None, False

        linter_logger.info("Persisting file masks: %s", self.file_mask)
        # Compare Templated with Raw
        diff_templ = SequenceMatcher(autojunk=None, a=self.file_mask[0], b=self.file_mask[1])
        bencher("fix_string: Match 0&1")
        diff_templ_codes = diff_templ.get_opcodes()
        linter_logger.debug("Templater diff codes: %s", diff_templ_codes)

        bencher("fix_string: Got Opcodes 0&1")
        # Compare Fixed with Templated
        diff_fix = SequenceMatcher(autojunk=None, a=self.file_mask[1], b=self.file_mask[2])
        bencher("fix_string: Matched 1&2")
        # diff_fix = SequenceMatcher(autojunk=None, a=self.file_mask[1][0], b=self.file_mask[2][0])
        diff_fix_codes = diff_fix.get_opcodes()
        linter_logger.debug("Fixing diff codes: %s", diff_fix_codes)
        bencher("fix_string: Got Opcodes 1&2")

        # If diff_templ isn't the same then we should just keep the template. If there *was*
        # a fix in that space, then we should raise an issue
        # If it is the same, then we can apply fixes as expected.
        write_buff = ''
        fixed_block = None
        templ_block = None
        # index in raw, templ and fix
        idx = (0, 0, 0)
        loop_idx = 0
        bencher("fix_string: Loop Setup")
        while True:
            loop_idx += 1
            linter_logger.debug("%04d: Write Loop: idx:%s, buff:%r", loop_idx, idx, write_buff)
            if templ_block is None:
                if diff_templ_codes:
                    templ_block = diff_templ_codes.pop(0)
                # We've exhausted the template. Have we exhausted the fixes?
                elif fixed_block is None and not diff_fix_codes:
                    # Yes - excellent. DONE
                    break
                # Deal with the case that we only have inserts left.
                elif all(elem[0] == 'insert' for elem in diff_fix_codes):
                    for fixed_block in diff_fix_codes:
                        write_buff += self.file_mask[2][fixed_block[3]:fixed_block[4]]
                    break
                else:
                    raise NotImplementedError("Fix Block(s) left over! Don't know how to handle this! aeflf8wh")
            if fixed_block is None:
                if diff_fix_codes:
                    fixed_block = diff_fix_codes.pop(0)
                elif templ_block[0] != 'delete':
                    # We need another fixed_block for the cases where templ_block[0] is not 'delete'
                    raise NotImplementedError(
                        "A {} template block remains with no more diff_fix_codes left".format(templ_block[0])
                    )

            linter_logger.debug("%04d: Blocks: template:%s, fix:%s", loop_idx, templ_block, fixed_block)
            if templ_block[0] == 'equal':
                if fixed_block[0] == 'equal':
                    # No templating, no fixes, go with middle and advance indexes
                    # Find out how far we can advance (we use the middle version because it's common)
                    if templ_block[4] == fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:fixed_block[2]]
                        # consume both blocks
                        fixed_block = None
                        templ_block = None
                    elif templ_block[4] > fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:fixed_block[2]]
                        # consume fixed block
                        fixed_block = None
                    elif templ_block[4] < fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:templ_block[4]]
                        # consume templ block
                        templ_block = None
                    idx = (idx[0] + len(buff), idx[1] + len(buff), idx[2] + len(buff))
                    write_buff += buff
                    continue
                elif fixed_block[0] == 'replace':
                    # Consider how to apply fixes.
                    # Can we implement the fix while staying in the equal segment?
                    if fixed_block[2] <= templ_block[4]:
                        # Yes! Write from the fixed version.
                        write_buff += self.file_mask[2][idx[2]:fixed_block[4]]
                        idx = (idx[0] + (fixed_block[2] - fixed_block[1]), fixed_block[2], fixed_block[4])
                        # Consume the fixed block because we've written the whole thing.
                        fixed_block = None
                        if not diff_templ_codes and not diff_fix_codes:
                            # If we just just used the last fixed_block and were using the last templ_block
                            # then consume the templ_block
                            templ_block = None
                        continue
                    else:
                        raise NotImplementedError("DEF")
                elif fixed_block[0] == 'delete':
                    # We're deleting items, nothing to write but we can consume some
                    # blocks and advance some indexes.
                    idx = (idx[0] + (fixed_block[2] - fixed_block[1]), fixed_block[2], fixed_block[4])
                    fixed_block = None
                elif fixed_block[0] == 'insert':
                    # We're inserting items, Write from the fix block, but only that index moves.
                    write_buff += self.file_mask[2][idx[2]:fixed_block[4]]
                    idx = (idx[0], idx[1], fixed_block[4])
                    fixed_block = None
                else:
                    raise NotImplementedError(
                        ("Unexpected opcode {0} for fix block! Please report this "
                         "issue on github with the query and rules you're trying to "
                         "fix.").format(fixed_block[0]))
            elif templ_block[0] == 'replace':
                # We're in a templated section - we should write the templated version.
                # we should consume the whole replace block and then deal with where
                # we end up.
                buff = self.file_mask[0][idx[0]:templ_block[2]]
                new_templ_idx = templ_block[4]

                # Fast forward through fix blocks until we catch up. We're not implementing
                # any changes in a templated section.
                while True:
                    if fixed_block[2] > new_templ_idx >= fixed_block[1]:
                        # this block contains the end point
                        break
                    else:
                        # We're not at the end point yet, continue to fast forward through.
                        if fixed_block[0] != 'equal':
                            print("WARNING: Skipping edit block: {0}".format(fixed_block))
                        if diff_fix_codes:
                            fixed_block = diff_fix_codes.pop(0)
                        else:
                            raise NotImplementedError("Unexpectedly depleted the fixes. Panic!")
                # Are we exactly on a join?
                if new_templ_idx == fixed_block[1]:
                    # GREAT - this makes things easy because we have an equality point already
                    idx = (templ_block[2], new_templ_idx, fixed_block[3])
                else:
                    if fixed_block[0] == 'equal':
                        # If it's in an equal block, we can use the same offset from the end.
                        idx = (templ_block[2], new_templ_idx, fixed_block[3] + (new_templ_idx - fixed_block[1]))
                    else:
                        # TODO: We're trying to move through an templated section, but end up
                        # in a fixed section. We've lost track of indexes.
                        # We might need to panic if this happens...
                        print("UMMMMMM!")
                        print(new_templ_idx)
                        print(fixed_block)
                        raise NotImplementedError("ABC")
                write_buff += buff
                # consume template block
                templ_block = None
            elif templ_block[0] == 'delete':
                # The comparison, things that the templater has deleted
                # some characters. This is just a quirk of the differ.
                # In reality this means we just write these characters
                # and don't worry about advancing the other indexes.
                buff = self.file_mask[0][idx[0]:templ_block[2]]
                # consume templ block
                templ_block = None
                idx = (idx[0] + len(buff), idx[1], idx[2])
                write_buff += buff
            elif templ_block[0] == 'insert':
                # The templater has inserted something here. We don't need
                # to write anything here (because whatever we're looking at
                # was inserted by the templater), but we do need to keep
                # track of what happened to the rest of the section we're in.
                # If nothing was fixed then it's easy because the indices
                # will be the same. Otherwise... great question...

                # For now let's just deal with the happy case where the fixed
                # block is equal
                if fixed_block[0] == 'equal':
                    # Let's make sure we can consume enough to get through the
                    # templ block and not get to the end of the fix block.
                    if templ_block[4] <= fixed_block[2]:
                        insert_len = templ_block[4] - templ_block[3]
                        idx = (idx[0], idx[1] + insert_len, idx[2] + insert_len)
                        # if things matched up perfectly, consume the fixed block
                        if templ_block[4] == fixed_block[2]:
                            fixed_block = None
                        # always consume templ block in this case
                        templ_block = None
                    else:
                        raise NotImplementedError(
                            ("Unexpected scenario during insert opcode! Please report "
                             "this issue on github with the query and rules you're trying "
                             "to fix."))
                else:
                    raise NotImplementedError(
                        ("Unexpected opcode {0} for fix block! Please report this "
                         "issue on github with the query and rules you're trying to "
                         "fix.").format(fixed_block[0]))
            else:
                raise NotImplementedError(
                    ("Unexpected opcode {0} for template block! Please report this "
                     "issue on github with the query and rules you're trying to "
                     "fix.").format(templ_block[0]))

        bencher("fix_string: Fixing loop done")
        # The success metric here is whether anything ACTUALLY changed.
        return write_buff, write_buff != self.file_mask[0]

    def persist_tree(self, suffix=''):
        """Persist changes to the given path.

        We use the file_mask to do a safe merge, avoiding any templated
        sections. First we need to detect where there have been changes
        between the fixed and templated versions.

        We use difflib.SequenceMatcher.get_opcodes
        See: https://docs.python.org/3.7/library/difflib.html#difflib.SequenceMatcher.get_opcodes
        It returns a list of tuples ('equal|replace', ia1, ia2, ib1, ib2).

        """
        write_buff, success = self.fix_string()

        if success:
            fname = self.path
            # If there is a suffix specified, then use it.s
            if suffix:
                root, ext = os.path.splitext(fname)
                fname = root + suffix + ext
            # Actually write the file.
            with open(fname, 'w') as f:
                f.write(write_buff)
        return success


class LintedPath:
    """A class to store the idea of a collection of linted files at a single start path."""
    def __init__(self, path):
        self.files = []
        self.path = path

    def add(self, file):
        """Add a file to this path."""
        self.files.append(file)

    def check_tuples(self, by_path=False):
        """Compress all the tuples into one list.

        NB: This is a little crude, as you can't tell which
        file the violations are from. Good for testing though.
        For more control set the `by_path` argument to true.
        """
        if by_path:
            return {file.path: file.check_tuples() for file in self.files}
        else:
            tuple_buffer = []
            for file in self.files:
                tuple_buffer += file.check_tuples()
            return tuple_buffer

    def num_violations(self, **kwargs):
        """Count the number of violations in the path."""
        return sum(file.num_violations(**kwargs) for file in self.files)

    def get_violations(self, **kwargs):
        """Return a list of violations in the path."""
        buff = []
        for file in self.files:
            buff += file.get_violations(**kwargs)
        return buff

    def violation_dict(self, **kwargs):
        """Return a dict of violations by file path."""
        return {file.path: file.get_violations(**kwargs) for file in self.files}

    def stats(self):
        """Return a dict containing linting stats about this path."""
        return dict(
            files=len(self.files),
            clean=sum(file.is_clean() for file in self.files),
            unclean=sum(not file.is_clean() for file in self.files),
            violations=sum(file.num_violations() for file in self.files)
        )

    def persist_changes(self, formatter=None, fixed_file_suffix='', **kwargs):
        """Persist changes to files in the given path.

        This also logs the output as we go using the formatter if present.
        """
        # Run all the fixes for all the files and return a dict
        buffer = {}
        for file in self.files:
            if file.num_violations(fixable=True, **kwargs) > 0:
                buffer[file.path] = file.persist_tree(suffix=fixed_file_suffix)
                result = buffer[file.path]
            else:
                buffer[file.path] = True
                result = 'SKIP'

            if formatter:
                formatter.dispatch_persist_filename(filename=file.path, result=result)
        return buffer


class LintingResult:
    """A class to represent the result of a linting operation.

    Notably this might be a collection of paths, all with multiple
    potential files within them.
    """

    def __init__(self):
        self.paths = []

    @staticmethod
    def sum_dicts(d1, d2):
        """Take the keys of two dictionaries and add them."""
        keys = set(d1.keys()) | set(d2.keys())
        return {key: d1.get(key, 0) + d2.get(key, 0) for key in keys}

    @staticmethod
    def combine_dicts(*d):
        """Take any set of dictionaries and combine them."""
        dict_buffer = {}
        for dct in d:
            dict_buffer.update(dct)
        return dict_buffer

    def add(self, path):
        """Add a new `LintedPath` to this result."""
        self.paths.append(path)

    def check_tuples(self, by_path=False):
        """Fetch all check_tuples from all contained `LintedPath` objects.

        Args:
            by_path (:obj:`bool`, optional): When False, all the check_tuples
                are aggregated into one flat list. When True, we return a `dict`
                of paths, each with it's own list of check_tuples. Defaults to False.

        """
        if by_path:
            buff = {}
            for path in self.paths:
                buff.update(path.check_tuples(by_path=by_path))
            return buff
        else:
            tuple_buffer = []
            for path in self.paths:
                tuple_buffer += path.check_tuples()
            return tuple_buffer

    def num_violations(self, **kwargs):
        """Count the number of violations in the result."""
        return sum(path.num_violations(**kwargs) for path in self.paths)

    def get_violations(self, **kwargs):
        """Return a list of violations in the result."""
        buff = []
        for path in self.paths:
            buff += path.get_violations(**kwargs)
        return buff

    def violation_dict(self, **kwargs):
        """Return a dict of paths and violations."""
        return self.combine_dicts(path.violation_dict(**kwargs) for path in self.paths)

    def stats(self):
        """Return a stats dictionary of this result."""
        all_stats = dict(files=0, clean=0, unclean=0, violations=0)
        for path in self.paths:
            all_stats = self.sum_dicts(path.stats(), all_stats)
        if all_stats['files'] > 0:
            all_stats['avg per file'] = all_stats['violations'] * 1.0 / all_stats['files']
            all_stats['unclean rate'] = all_stats['unclean'] * 1.0 / all_stats['files']
        else:
            all_stats['avg per file'] = 0
            all_stats['unclean rate'] = 0
        all_stats['clean files'] = all_stats['clean']
        all_stats['unclean files'] = all_stats['unclean']
        all_stats['exit code'] = 65 if all_stats['violations'] > 0 else 0
        all_stats['status'] = 'FAIL' if all_stats['violations'] > 0 else 'PASS'
        return all_stats

    def as_records(self):
        """Return the result as a list of dictionaries.

        Each record contains a key specifying the filepath, and a list of violations. This
        method is useful for serialization as all objects will be builtin python types
        (ints, strs).
        """
        return [
            {'filepath': path, 'violations': [v.get_info_dict() for v in violations]}
            for lintedpath in self.paths
            for path, violations in lintedpath.violation_dict().items()
            if violations
        ]

    def persist_changes(self, formatter=None, **kwargs):
        """Run all the fixes for all the files and return a dict."""
        return self.combine_dicts(
            *[
                path.persist_changes(formatter=formatter, **kwargs)
                for path in self.paths
            ]
        )


class Linter:
    """The interface class to interact with the linter."""

    def __init__(self, sql_exts=('.sql',),
                 config=None, formatter=None):
        if config is None:
            raise ValueError("No config object provided to linter!")
        self.dialect = config.get('dialect_obj')
        self.templater = config.get('templater_obj')
        self.sql_exts = sql_exts
        # Store the config object
        self.config = config
        # Store the formatter for output
        self.formatter = formatter

    def get_ruleset(self, config=None):
        """Get hold of a set of rules."""
        rs = get_ruleset()
        cfg = config or self.config
        return rs.get_rulelist(config=cfg)

    def rule_tuples(self):
        """A simple pass through to access the rule tuples of the rule set."""
        rs = self.get_ruleset()
        return [(rule.code, rule.description) for rule in rs]

    def parse_string(self, s, fname=None, recurse=True, config=None):
        """Parse a string.

        Returns:
            `tuple` of (`parsed`, `violations`, `time_dict`, `config_diff`).
                `parsed` is a segment structure representing the parsed file. If
                    parsing fails due to an inrecoverable violation then we will
                    return None.
                `violations` is a :obj:`list` of violations so far, which will either be
                    templating, lexing or parsing violations at this stage.
                `time_dict` is a :obj:`dict` containing timings for how long each step
                    took in the process.

        """
        violations = []
        t0 = time.monotonic()
        bencher = BenchIt()  # starts the timer
        if fname:
            short_fname = fname.replace('\\', '/').split('/')[-1]
        else:
            # this handles to potential case of a null fname
            short_fname = fname
        bencher("Staring parse_string for {0!r}".format(short_fname))

        # Dispatch the output for the parse header (including the config diff)
        if self.formatter:
            self.formatter.dispatch_parse_header(fname, self.config, config)

        linter_logger.info("TEMPLATING RAW [%s] (%s)", self.templater.name, fname)
        s, templater_violations = self.templater.process(s, fname=fname, config=config or self.config)
        violations += templater_violations
        # Detect the case of a catastrophic templater fail. In this case
        # we don't continue. We'll just bow out now.
        if not s:
            file_segment = None

        t1 = time.monotonic()
        bencher("Templating {0!r}".format(short_fname))

        if s:
            linter_logger.info("LEXING RAW (%s)", fname)
            # Lex the file and log any problems
            try:
                file_segment, lex_vs = FileSegment.from_raw(s, config=config or self.config)
                # We might just get the violations as a list
                violations += lex_vs
            except SQLLexError as err:
                violations.append(err)
                file_segment = None
        else:
            file_segment = None

        if file_segment:
            linter_logger.info(file_segment.stringify())

        t2 = time.monotonic()
        bencher("Lexing {0!r}".format(short_fname))
        linter_logger.info("PARSING (%s)", fname)
        # Parse the file and log any problems
        if file_segment:
            try:
                # Make a parse context and parse
                with RootParseContext.from_config(config=config or self.config, recurse=recurse) as ctx:
                    parsed = file_segment.parse(parse_context=ctx)
            except SQLParseError as err:
                violations.append(err)
                parsed = None
            if parsed:
                linter_logger.info("\n###\n#\n# {0}\n#\n###".format("Parsed Tree:"))
                linter_logger.info("\n" + parsed.stringify())
                # We may succeed parsing, but still have unparsable segments. Extract them here.
                for unparsable in parsed.iter_unparsables():
                    # No exception has been raised explicitly, but we still create one here
                    # so that we can use the common interface
                    violations.append(
                        SQLParseError(
                            "Found unparsable section: {0!r}".format(
                                unparsable.raw if len(unparsable.raw) < 40 else unparsable.raw[:40] + "..."),
                            segment=unparsable
                        )
                    )
                    linter_logger.info("Found unparsable segment...")
                    linter_logger.info(unparsable.stringify())
        else:
            parsed = None

        t3 = time.monotonic()
        time_dict = {'templating': t1 - t0, 'lexing': t2 - t1, 'parsing': t3 - t2}
        bencher("Finish parsing {0!r}".format(short_fname))
        return parsed, violations, time_dict

    @staticmethod
    def extract_ignore_from_comment(comment):
        """Extract ignore mask entries from a comment segment."""
        # Also trim any whitespace afterward
        comment_content = comment.raw_trimmed().strip()
        if comment_content.startswith('noqa'):
            # This is an ignore identifier
            comment_remainder = comment_content[4:]
            if comment_remainder:
                if not comment_remainder.startswith(':'):
                    return SQLParseError(
                        "Malformed 'noqa' section. Expected 'noqa: <rule>[,...]",
                        segment=comment
                    )
                comment_remainder = comment_remainder[1:]
                rules = [r.strip() for r in comment_remainder.split(',')]
                return (comment.pos_marker.line_no, tuple(rules))
            else:
                return (comment.pos_marker.line_no, None)
        return None

    def lint_string(self, s, fname='<string input>', fix=False, config=None):
        """Lint a string.

        Returns:
            :obj:`LintedFile`: an object representing that linted file.

        """
        # Before templating, we want to get a store of what's in the file
        # so we can compare later. We iterate character by character because
        # we don't want to miss anything.
        raw_buff = s

        # Sort out config, defaulting to the built in config if no override
        config = config or self.config

        # Using the new parser, read the file object.
        parsed, vs, time_dict = self.parse_string(s=s, fname=fname, config=config)

        # Look for comment segments which might indicate lines to ignore.
        ignore_buff = []
        if parsed:
            for comment in parsed.recursive_crawl('comment'):
                if comment.name == 'inline_comment':
                    ignore_entry = self.extract_ignore_from_comment(comment)
                    if isinstance(ignore_entry, SQLParseError):
                        vs.append(ignore_entry)
                    elif ignore_entry:
                        ignore_buff.append(ignore_entry)
            if ignore_buff:
                linter_logger.info("Parsed noqa directives from file: %r", ignore_buff)

        templ_buff = None
        fixed_buff = None
        if parsed:
            # Store the templated version
            templ_buff = parsed.raw
            t0 = time.monotonic()
            linter_logger.info("LINTING (%s)", fname)
            # Get the initial violations
            linting_errors = []
            for crawler in self.get_ruleset(config=config):
                lerrs, _, _, _ = crawler.crawl(parsed, dialect=config.get('dialect_obj'))
                linting_errors += lerrs
            initial_linting_errors = linting_errors

            # If we're in fix mode, iteratively apply fixes until done, or we can't make a move.
            if fix:
                # If we're in fix mode, then we need to progressively call and reconstruct
                working = parsed
                # Keep a set of previous versions to catch infinite loops.
                previous_versions = {working.raw}
                linting_errors = []
                last_fixes = None
                while True:
                    changed = False
                    for crawler in self.get_ruleset(config=config):
                        # fixes should be a dict {} with keys edit, delete, create
                        # delete is just a list of segments to delete
                        # edit and create are list of tuples. The first element is the
                        # "anchor", the segment to look for either to edit or to insert BEFORE.
                        # The second is the element to insert or create.

                        lerrs, _, fixes, _ = crawler.crawl(working, dialect=config.get('dialect_obj'), fix=True)
                        linting_errors += lerrs
                        if fixes:
                            linter_logger.info("Applying Fixes: %s", fixes)

                            if last_fixes and fixes == last_fixes:
                                linter_logger.warning(
                                    "One fix for %s not applied, it would re-cause the same error.",
                                    crawler.code)
                            else:
                                last_fixes = fixes
                                new_working, fixes = working.apply_fixes(fixes)

                                # Check for infinite loops
                                if new_working.raw not in previous_versions:
                                    working = new_working
                                    previous_versions.add(working.raw)
                                    changed = True
                                else:
                                    linter_logger.warning(
                                        "One fix for %s not applied, it would re-cause the same error.",
                                        crawler.code)
                    if not changed:
                        # The file is clean :)
                        break
                # Set things up to return the altered version
                parsed = working

            # Update the timing dict
            t1 = time.monotonic()
            time_dict['linting'] = t1 - t0

            # We're only going to return the *initial* errors, rather
            # than any generated during the fixing cycle.
            vs += initial_linting_errors
            fixed_buff = parsed.raw

        # We process the ignore config here if appropriate
        if config:
            for violation in vs:
                violation.ignore_if_in(config.get('ignore'))

        file_mask = (raw_buff, templ_buff, fixed_buff)
        linted_file = LintedFile(fname, vs, time_dict, parsed,
                                 file_mask=file_mask, ignore_mask=ignore_buff)

        # This is the main command line output from linting.
        if self.formatter:
            self.formatter.dispatch_file_violations(fname, linted_file, only_fixable=fix)

        # Safety flag for unset dialects
        if config.get('dialect') == 'ansi' and linted_file.get_violations(fixable=True if fix else None, types=SQLParseError):
            if self.formatter:
                self.formatter.dispatch_dialect_warning()

        return linted_file

    def _get_potential_ignore_file_locations_for_file(self, path, ignore_file_name='.sqlfluffignore', current_dir=Path.cwd()):
        """Returns a set of potential paths for ignore files based on file to be linted.

        When a path to a file to be linted is explicitly passed
        we look for ignore files in all directories that are parents of the file,
        up to the current directory.

        If the current directory is not a parent of the file we only
        look for an ignore file in the direct parent of the file.

        """
        path = Path(path)

        ignore_set = set()
        if current_dir in path.parents:
            parents = path.relative_to(current_dir).parents
            root = current_dir
        else:
            parents = [path.parent.relative_to(path.root)]
            root = path.root

        for directory in parents:
            abs_dir = root / directory
            files = [x for x in abs_dir.iterdir() if x.is_file()]
            for f in files:
                # Handle potential .sqlfluffignore files
                if f.name == ignore_file_name:
                    with open(f, 'r') as fh:
                        spec = pathspec.PathSpec.from_lines('gitwildmatch', fh)
                    matches = spec.match_tree(abs_dir)
                    for m in matches:
                        ignore_path = abs_dir / m
                        ignore_set.add(str(ignore_path))
                    # We don't need to process the ignore file any futher
                    continue
        return ignore_set

    def paths_from_path(self, path, ignore_file_name='.sqlfluffignore', ignore_non_existent_files=False, ignore_files=True, current_dir=Path.cwd()):
        """Return a set of sql file paths from a potentially more ambigious path string.

        Here we also deal with the .sqlfluffignore file if present.

        """
        if not os.path.exists(path):
            if ignore_non_existent_files:
                return []
            else:
                raise IOError("Specified path does not exist")

        # Files referred to exactly are also ignored if
        # matched, but we warn the users when that happens
        is_exact_file = not os.path.isdir(path)

        ignore_set = set()
        if is_exact_file:
            dirpath = os.path.dirname(path)
            files = [os.path.basename(path)]
            path_walk = [
                (dirpath, None, files)
            ]
            ignore_set = self._get_potential_ignore_file_locations_for_file(
                path=path,
                ignore_file_name=ignore_file_name,
                current_dir=current_dir,
            )
        else:
            path_walk = os.walk(path)

        # If it's a directory then expand the path!
        buffer = []
        for dirpath, _, filenames in path_walk:
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                # Handle potential .sqlfluffignore files
                if ignore_files and fname == ignore_file_name:
                    with open(fpath, 'r') as fh:
                        spec = pathspec.PathSpec.from_lines('gitwildmatch', fh)
                    matches = spec.match_tree(dirpath)
                    for m in matches:
                        ignore_path = os.path.join(dirpath, m)
                        ignore_set.add(ignore_path)
                    # We don't need to process the ignore file any futher
                    continue

                # We won't purge files *here* because there's an edge case
                # that the ignore file is processed after the sql file.

                # Scan for remaining files
                for ext in self.sql_exts:
                    # is it a sql file?
                    if fname.endswith(ext):
                        buffer.append(fpath)

        if not ignore_files:
            return sorted(buffer)

        # Check the buffer for ignore items and normalise the rest.
        filtered_buffer = []

        for fpath in buffer:
            if fpath not in ignore_set:
                filtered_buffer.append(os.path.normpath(fpath))
            elif is_exact_file:
                print(
                    "WARNING: Exact file path %s was given but "
                    "it was ignored by a %s pattern, "
                    "re-run with `--not-ignore-files` to "
                    "skip %s" % (
                        path,
                        ignore_file_name,
                        ignore_file_name,
                    )
                )

        # Return
        return sorted(filtered_buffer)

    def lint_string_wrapped(self, string, fname='<string input>', fix=False):
        """Lint strings directly."""
        result = LintingResult()
        linted_path = LintedPath(fname)
        linted_path.add(
            self.lint_string(string, fname=fname, fix=fix)
        )
        result.add(linted_path)
        return result

    def lint_path(self, path, fix=False, ignore_non_existent_files=False, ignore_files=True):
        """Lint a path."""
        linted_path = LintedPath(path)
        if self.formatter:
            self.formatter.dispatch_path(path)
        for fname in self.paths_from_path(path, ignore_non_existent_files=ignore_non_existent_files, ignore_files=ignore_files):
            config = self.config.make_child_from_path(fname)
            # Handle unicode issues gracefully
            with open(fname, 'r', encoding='utf8', errors='backslashreplace') as target_file:
                linted_path.add(
                    self.lint_string(target_file.read(), fname=fname,
                                     fix=fix, config=config))
        return linted_path

    def lint_paths(self, paths, fix=False, ignore_non_existent_files=False, ignore_files=True):
        """Lint an iterable of paths."""
        # If no paths specified - assume local
        if len(paths) == 0:
            paths = (os.getcwd(),)
        # Set up the result to hold what we get back
        result = LintingResult()
        for path in paths:
            # Iterate through files recursively in the specified directory (if it's a directory)
            # or read the file directly if it's not
            result.add(self.lint_path(path, fix=fix,
                                      ignore_non_existent_files=ignore_non_existent_files, ignore_files=ignore_files))
        return result

    def parse_path(self, path, recurse=True):
        """Parse a path of sql files.

        NB: This a generator which will yield the result of each file
        within the path iteratively.
        """
        for fname in self.paths_from_path(path):
            if self.formatter:
                self.formatter.dispatch_path(path)
            config = self.config.make_child_from_path(fname)
            # Handle unicode issues gracefully
            with open(fname, 'r', encoding='utf8', errors='backslashreplace') as target_file:
                yield (
                    *self.parse_string(target_file.read(), fname=fname,
                                       recurse=recurse, config=config),
                    # Also yield the config
                    config
                )
