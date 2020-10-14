"""The Test file for CLI (General)."""

import configparser
import tempfile
import os
import shutil
import json
import oyaml as yaml
import subprocess

# Testing libraries
import pytest
from click.testing import CliRunner

# We import the library directly here to get the version
import sqlfluff
from sqlfluff.cli.commands import lint, version, rules, fix, parse, dialects


def invoke_assert_code(ret_code=0, args=None, kwargs=None, cli_input=None):
    """Invoke a command and check return code."""
    args = args or []
    kwargs = kwargs or {}
    if cli_input:
        kwargs['input'] = cli_input
    runner = CliRunner()
    result = runner.invoke(*args, **kwargs)
    # Output the CLI code for debugging
    print(result.output)
    # Check return codes
    if ret_code == 0:
        if result.exception:
            raise result.exception
    assert ret_code == result.exit_code
    return result


def test__cli__command_directed():
    """Basic checking of lint functionality."""
    result = invoke_assert_code(
        ret_code=65,
        args=[lint, ['-n', 'test/fixtures/linter/indentation_error_simple.sql']]
    )
    # We should get a readout of what the error was
    check_a = "L:   2 | P:   4 | L003"
    # NB: Skip the number at the end because it's configurable
    check_b = "Indentation"
    assert check_a in result.output
    assert check_b in result.output


def test__cli__command_dialect():
    """Check the script raises the right exception on an unknown dialect."""
    # The dialect is unknown should be a non-zero exit code
    invoke_assert_code(
        ret_code=66,
        args=[lint, ['-n', '--dialect', 'faslkjh', 'test/fixtures/linter/indentation_error_simple.sql']]
    )


@pytest.mark.parametrize('command', [
    ('-', '-n', ), ('-', '-n', '-v',), ('-', '-n', '-vv',), ('-', '-vv',),
])
def test__cli__command_lint_stdin(command):
    """Check basic commands on a simple script using stdin.

    The subprocess command should exit without errors, as no issues should be found.
    """
    with open('test/fixtures/cli/passing_a.sql', 'r') as test_file:
        sql = test_file.read()
    invoke_assert_code(args=[lint, command], cli_input=sql)


@pytest.mark.parametrize('command', [
    # Test basic linting
    (lint, ['-n', 'test/fixtures/cli/passing_b.sql']),
    # Original tests from test__cli__command_lint
    (lint, ['-n', 'test/fixtures/cli/passing_a.sql']),
    (lint, ['-n', '-v', 'test/fixtures/cli/passing_a.sql']),
    (lint, ['-n', '-vvvv', 'test/fixtures/cli/passing_a.sql']),
    (lint, ['-vvvv', 'test/fixtures/cli/passing_a.sql']),
    # Test basic linting with very high verbosity
    (lint, ['-n', 'test/fixtures/cli/passing_b.sql', '-vvvvvvvvvvv']),
    # Test basic linting with specific logger
    (lint, ['-n', 'test/fixtures/cli/passing_b.sql', '-vvv', '--logger', 'parser']),
    # Check basic parsing
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql']),
    # Test basic parsing with very high verbosity
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '-vvvvvvvvvvv']),
    # Check basic parsing, with the code only option
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '-c']),
    # Check basic parsing, with the yaml output
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '-c', '-f', 'yaml']),
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '--format', 'yaml']),
    # Check the profiler and benching commands
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '--profiler']),
    (parse, ['-n', 'test/fixtures/cli/passing_b.sql', '--bench']),
    # Check linting works in specifying rules
    (lint, ['-n', '--rules', 'L001', 'test/fixtures/linter/operator_errors.sql']),
    # Check linting works in specifying multiple rules
    (lint, ['-n', '--rules', 'L001,L002', 'test/fixtures/linter/operator_errors.sql']),
    # Check linting works with both included and excluded rules
    (lint, ['-n', '--rules', 'L001,L006', '--exclude-rules', 'L006', 'test/fixtures/linter/operator_errors.sql']),
    # Check linting works with just excluded rules
    (lint, ['-n', '--exclude-rules', 'L006,L007', 'test/fixtures/linter/operator_errors.sql']),
    # Check the script doesn't raise an unexpected exception with badly formed files.
    (fix, ['--rules', 'L001', 'test/fixtures/cli/fail_many.sql', '-vvvvvvv'], 'y'),
    # Fix with a suffixs
    (fix, ['--rules', 'L001', '--fixed-suffix', '_fix', 'test/fixtures/cli/fail_many.sql']),
    # Fix without safety
    (fix, ['--no-safety', '--fixed-suffix', '_fix', 'test/fixtures/cli/fail_many.sql']),
    # Check that ignoring works (also checks that unicode files parse).
    (lint, ['-n', '--exclude-rules', 'L003,L009', '--ignore', 'parsing,lexing', 'test/fixtures/linter/parse_lex_error.sql']),
    # Check nofail works
    (lint, ['--nofail', 'test/fixtures/linter/parse_lex_error.sql']),
])
def test__cli__command_lint_parse(command):
    """Check basic commands on a more complicated script."""
    invoke_assert_code(args=command)


def test__cli__command_lint_warning_explicit_file_ignored():
    """Check ignoring file works when passed explicitly and ignore file is in the same directory."""
    runner = CliRunner()
    result = runner.invoke(lint, ['test/fixtures/linter/sqlfluffignore/path_b/query_c.sql'])
    assert result.exit_code == 0
    assert ("WARNING: Exact file path test/fixtures/linter/sqlfluffignore/path_b/query_c.sql "
            "was given but it was ignored") in result.output.strip()


def test__cli__command_lint_skip_ignore_files():
    """Check "ignore file" is skipped when --not-ignore-files flag is set."""
    runner = CliRunner()
    result = runner.invoke(
        lint,
        [
            'test/fixtures/linter/sqlfluffignore/path_b/query_c.sql',
            '--not-ignore-files'
        ])
    assert result.exit_code == 65
    assert "L009" in result.output.strip()


def test__cli__command_versioning():
    """Check version command."""
    # Get the package version info
    pkg_version = sqlfluff.__version__
    # Get the version info from the config file
    config = configparser.ConfigParser()
    config.read_file(open('src/sqlfluff/config.ini'))
    config_version = config['sqlfluff']['version']
    assert pkg_version == config_version
    # Get the version from the cli
    runner = CliRunner()
    result = runner.invoke(version)
    assert result.exit_code == 0
    # We need to strip to remove the newline characters
    assert result.output.strip() == pkg_version


def test__cli__command_version():
    """Just check version command for exceptions."""
    # Get the package version info
    pkg_version = sqlfluff.__version__
    runner = CliRunner()
    result = runner.invoke(version)
    assert result.exit_code == 0
    assert pkg_version in result.output
    # Check a verbose version
    result = runner.invoke(version, ['-v'])
    assert result.exit_code == 0
    assert pkg_version in result.output


def test__cli__command_rules():
    """Check rules command for exceptions."""
    invoke_assert_code(args=[rules])


def test__cli__command_dialects():
    """Check dialects command for exceptions."""
    invoke_assert_code(args=[dialects])


def generic_roundtrip_test(source_file, rulestring, final_exit_code=0, force=True, fix_input=None, fix_exit_code=0):
    """A test for roundtrip testing, take a file buffer, lint, fix and lint.

    This is explicitly different from the linter version of this, in that
    it uses the command line rather than the direct api.
    """
    filename = 'testing.sql'
    # Lets get the path of a file to use
    tempdir_path = tempfile.mkdtemp()
    filepath = os.path.join(tempdir_path, filename)
    # Open the example file and write the content to it
    with open(filepath, mode='w') as dest_file:
        for line in source_file:
            dest_file.write(line)
    # Check that we first detect the issue
    invoke_assert_code(ret_code=65, args=[lint, ['--rules', rulestring, filepath]])
    # Fix the file (in force mode)
    if force:
        fix_args = ['--rules', rulestring, '-f', filepath]
    else:
        fix_args = ['--rules', rulestring, filepath]
    invoke_assert_code(ret_code=fix_exit_code, args=[fix, fix_args], cli_input=fix_input)
    # Now lint the file and check for exceptions
    invoke_assert_code(ret_code=final_exit_code, args=[lint, ['--rules', rulestring, filepath]])
    shutil.rmtree(tempdir_path)


@pytest.mark.parametrize('rule,fname', [
    ('L001', 'test/fixtures/linter/indentation_errors.sql'),
    ('L008', 'test/fixtures/linter/whitespace_errors.sql'),
    ('L008', 'test/fixtures/linter/indentation_errors.sql'),
    # Really stretching the ability of the fixer to re-indent a file
    ('L003', 'test/fixtures/linter/indentation_error_hard.sql')
])
def test__cli__command__fix(rule, fname):
    """Test the round trip of detecting, fixing and then not detecting the rule."""
    with open(fname, mode='r') as test_file:
        generic_roundtrip_test(test_file, rule)


# Test case disabled because there isn't a good example of where to test this.
# This *should* test the case where a rule DOES have a proposed fix, but for
# some reason when we try to apply it, there's a failure.
# @pytest.mark.parametrize('rule,fname', [
#     # NB: L004 currently has no fix routine.
#     ('L004', 'test/fixtures/linter/indentation_errors.sql')
# ])
# def test__cli__command__fix_fail(rule, fname):
#     """Test the round trip of detecting, fixing and then still detecting the rule."""
#     with open(fname, mode='r') as test_file:
#         generic_roundtrip_test(test_file, rule, fix_exit_code=1, final_exit_code=65)


@pytest.mark.parametrize('stdin,rules,stdout', [
    ('select * from t', 'L003', 'select * from t'),  # no change
    (' select * from t', 'L003', 'select * from t'),  # fix preceding whitespace
    # L031 fix aliases in joins
    ('SELECT u.id, c.first_name, c.last_name, COUNT(o.user_id) FROM users as u JOIN customers as c on u.id = c.user_id JOIN orders as o on u.id = o.user_id;',
     'L031',
     'SELECT u.id, customers.first_name, customers.last_name, COUNT(orders.user_id) FROM users as u JOIN customers on u.id = customers.user_id JOIN orders on u.id = orders.user_id;'),
])
def test__cli__command_fix_stdin(stdin, rules, stdout):
    """Check stdin input for fix works."""
    result = invoke_assert_code(args=[fix, ('-', '--rules', rules)], cli_input=stdin)
    assert result.output == stdout


@pytest.mark.parametrize('rule,fname,prompt,exit_code', [
    ('L001', 'test/fixtures/linter/indentation_errors.sql', 'y', 0),
    ('L001', 'test/fixtures/linter/indentation_errors.sql', 'n', 65)
])
def test__cli__command__fix_no_force(rule, fname, prompt, exit_code):
    """Round trip test, using the prompts."""
    with open(fname, mode='r') as test_file:
        generic_roundtrip_test(
            test_file, rule, force=False, final_exit_code=exit_code,
            fix_input=prompt)


@pytest.mark.parametrize('serialize', ['yaml', 'json'])
def test__cli__command_parse_serialize_from_stdin(serialize):
    """Check that the parser serialized output option is working.

    Not going to test for the content of the output as that is subject to change.
    """
    result = invoke_assert_code(
        args=[parse, ('-', '--format', serialize)],
        cli_input='select * from tbl',
    )
    if serialize == 'json':
        result = json.loads(result.output)
    elif serialize == 'yaml':
        result = yaml.safe_load(result.output)
    else:
        raise Exception
    result = result[0]  # only one file
    assert result['filepath'] == 'stdin'


@pytest.mark.parametrize('serialize', ['yaml', 'json'])
@pytest.mark.parametrize('sql,expected,exit_code', [
    ('select * from tbl', [], 0),  # empty list if no violations
    (
        'SElect * from tbl',
        [{
            "filepath": "stdin",
            "violations": [
                {
                    "code": "L010",
                    "line_no": 1,
                    "line_pos": 1,
                    "description": "Inconsistent capitalisation of keywords."
                }
            ]
        }],
        65
    )
])
def test__cli__command_lint_serialize_from_stdin(serialize, sql, expected, exit_code):
    """Check an explicit serialized return value for a single error."""
    result = invoke_assert_code(
        args=[lint, ('-', '--rules', 'L010', '--format', serialize)],
        cli_input=sql,
        ret_code=exit_code
    )

    if serialize == 'json':
        assert json.loads(result.output) == expected
    elif serialize == 'yaml':
        assert yaml.safe_load(result.output) == expected
    else:
        raise Exception


@pytest.mark.parametrize('command', [
    [lint, ('this_file_does_not_exist.sql')],
    [fix, ('this_file_does_not_exist.sql', '--no-safety')]
])
def test__cli__command_fail_nice_not_found(command):
    """Check commands fail as expected when then don't find files."""
    result = invoke_assert_code(
        args=command,
        ret_code=1
    )
    assert 'could not be accessed' in result.output


@pytest.mark.parametrize('serialize', ['yaml', 'json'])
def test__cli__command_lint_serialize_multiple_files(serialize):
    """Check the general format of JSON output for multiple files."""
    fpath = 'test/fixtures/linter/indentation_errors.sql'

    # note the file is in here twice. two files = two payloads.
    result = invoke_assert_code(
        args=[lint, (fpath, fpath, '--format', serialize)],
        ret_code=65,
    )

    if serialize == 'json':
        result = json.loads(result.output)
    elif serialize == 'yaml':
        result = yaml.safe_load(result.output)
    else:
        raise Exception
    assert len(result) == 2


def test___main___help():
    """Test that the CLI can be access via __main__."""
    # nonzero exit is good enough
    subprocess.check_output(['python', '-m', 'sqlfluff', '--help'])
