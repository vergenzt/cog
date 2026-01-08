import argparse
import contextlib
import copy
from enum import Enum, StrEnum, auto
from fileinput import FileInput, isstdin
import glob
import os
from dataclasses import InitVar, dataclass, field, replace
from pathlib import Path
import shlex
import sys
from textwrap import dedent
from types import MethodType
from typing import (
    ClassVar,
    Dict,
    Iterator,
    List,
    Literal,
    NoReturn,
    Optional,
    TextIO,
    TypeVar,
)

from .errors import CogUsageError

description = """\
cog - generate content with inlined Python code.

cog [OPTIONS] [INFILE | @FILELIST | &FILELIST] ...
"""


class _NonEarlyExitingArgumentParser(argparse.ArgumentParser):
    """
    Work around https://github.com/python/cpython/issues/121018
    (Upstream fix is available in Python 3.12+)
    """

    def error(self, message: str) -> NoReturn:
        raise CogUsageError(message)


class _UpdateDictAction(argparse.Action):
    """
    Updates dest with dictionary values for each `<key>=<value>` argument.
    """

    @staticmethod
    def _parse_define(arg):
        if arg.count("=") < 1:
            raise argparse.ArgumentTypeError("takes a name=value argument")
        return arg.split("=", 1)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw, type=self._parse_define)

    def __call__(self, _parser, ns, arg, _option_string=None):
        getattr(ns, self.dest).update([arg])


@dataclass(frozen=True)
class Markers:
    begin_spec: str
    end_spec: str
    end_output: str

    @classmethod
    def from_arg(cls, arg: str):
        parts = arg.split(" ")
        if len(parts) != 3:
            # tell argparse to prefix our error message with the option string
            raise argparse.ArgumentTypeError(
                f"requires 3 values separated by spaces, could not parse {arg!r}"
            )
        return cls(*parts)


class FileListType(StrEnum):
    PLAIN = "@"
    CHDIR = "&"


@dataclass(frozen=True)
class CogInput:
    filestr: str
    filelist_type: FileListType | None = None

    @classmethod
    def from_arg(cls, filearg: str) -> "CogInput":
        if filearg[0] in FileListType:
            return cls(filearg[1:], FileListType(filearg[0]))
        else:
            return cls(filearg)


@dataclass(frozen=True)
class CogResolvedInput:
    filename: str
    options: "CogOptions"


@dataclass(frozen=True)
class CogOptions:
    """Options for a run of cog."""

    _parser: ClassVar = _NonEarlyExitingArgumentParser(
        prog="cog",
        usage=argparse.SUPPRESS,
        description=description,
        exit_on_error=False,  # doesn't always work until 3.12+; see workaround above
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    inputs: List[CogInput] = field(default_factory=list)
    _parser.add_argument(
        "inputs",
        metavar="[INFILE | @FILELIST | &FILELIST]",
        nargs=argparse.ZERO_OR_MORE,
        type=CogInput.from_arg,
        help=dedent("""
            FILELIST is the name of a text file containing file names or
            other @FILELISTs.

            For @FILELIST, paths in the file list are relative to the working
            directory where cog was called.  For &FILELIST, paths in the file
            list are relative to the file list location."
        """),
    )

    # add unexposed chdir option to support &FILELIST args
    chdir: Path = Path(".")

    hash_output: bool = False
    _parser.add_argument(
        "-c",
        dest="hash_output",
        action="store_true",
        help="Checksum the output to protect it against accidental change.",
    )

    delete_code: bool = False
    _parser.add_argument(
        "-d",
        dest="delete_code",
        action="store_true",
        help="Delete the Python code from the output file.",
    )

    defines: Dict[str, str] = field(default_factory=dict)
    _parser.add_argument(
        "-D",
        dest="defines",
        metavar="name=val",
        action=_UpdateDictAction,
        help="Define a global string available to your Python code.",
    )

    warn_empty: bool = False
    _parser.add_argument(
        "-e",
        dest="warn_empty",
        action="store_true",
        help="Warn if a file has no cog code in it.",
    )

    include_path: List[str] = field(default_factory=list)
    _parser.add_argument(
        "-I",
        dest="include_path",
        metavar="PATH",
        type=lambda paths: map(os.path.abspath, paths.split(os.path.pathsep)),
        action="extend",
        help="Add PATH to the list of directories for data files and modules.",
    )

    encoding: str = "utf-8"
    _parser.add_argument(
        "-n",
        dest="encoding",
        metavar="ENCODING",
        help="Use ENCODING when reading and writing files.",
    )

    output_name: Optional[str] = None
    _parser.add_argument(
        "-o",
        dest="output_name",
        metavar="OUTNAME",
        help="Write the output to OUTNAME.",
    )

    prologue: str = ""
    _parser.add_argument(
        "-p",
        dest="prologue",
        help=dedent("""
            Prepend the Python source with PROLOGUE. Useful to insert an import
            line. Example: -p "import math"
        """),
    )

    print_output: bool = False
    _parser.add_argument(
        "-P",
        dest="print_output",
        action="store_true",
        help="Use print() instead of cog.outl() for code output.",
    )

    replace: bool = False
    _parser.add_argument(
        "-r",
        dest="replace",
        action="store_true",
        help="Replace the input file with the output.",
    )

    suffix: Optional[str] = None
    _parser.add_argument(
        "-s",
        dest="suffix",
        metavar="STRING",
        help="Suffix all generated output lines with STRING.",
    )

    newline: str | None = None
    _parser.add_argument(
        "-U",
        dest="newline",
        action="store_const",
        const="\n",
        help="Write the output with Unix newlines (only LF line-endings).",
    )

    make_writable_cmd: Optional[str] = None
    _parser.add_argument(
        "-w",
        dest="make_writable_cmd",
        metavar="CMD",
        help=dedent("""
            Use CMD if the output file needs to be made writable. A %%s in the CMD
            will be filled with the filename.
        """),
    )

    no_generate: bool = False
    _parser.add_argument(
        "-x",
        dest="no_generate",
        action="store_true",
        help="Excise all the generated output without running the Pythons.",
    )

    eof_can_be_end: bool = False
    _parser.add_argument(
        "-z",
        dest="eof_can_be_end",
        action="store_true",
        help="The end-output marker can be omitted, and is assumed at eof.",
    )

    show_version: bool = False
    _parser.add_argument(
        "-v",
        dest="show_version",
        action="store_true",
        help="Print the version of cog and exit.",
    )

    check: bool = False
    _parser.add_argument(
        "--check",
        action="store_true",
        help="Check that the files would not change if run again.",
    )

    check_fail_msg: str | None = None
    _parser.add_argument(
        "--check-fail-msg",
        metavar="MSG",
        help="If --check fails, include MSG in the output to help devs understand how to run cog in your project.",
    )

    diff: bool = False
    _parser.add_argument(
        "--diff",
        action="store_true",
        help="With --check, show a diff of what failed the check.",
    )

    markers: Markers = Markers("[[[cog", "]]]", "[[[end]]]")
    _parser.add_argument(
        "--markers",
        metavar="'START END END-OUTPUT'",
        type=Markers.from_arg,
        help=dedent("""
            The patterns surrounding cog inline instructions. Should include three
            values separated by spaces, the start, end, and end-output markers.
            Defaults to '[[[cog ]]] [[[end]]]'.
        """),
    )

    # helper delegates
    begin_spec = property(lambda self: self.markers.begin_spec)
    end_spec = property(lambda self: self.markers.end_spec)
    end_output = property(lambda self: self.markers.end_output)

    verbosity: int = 2
    _parser.add_argument(
        "--verbosity",
        type=int,
        help=dedent("""
            Control the amount of output. 2 (the default) lists all files, 1 lists
            only changed files, 0 lists no files.
        """),
    )

    _parser.add_argument("-?", action="help", help=argparse.SUPPRESS)

    @classmethod
    def format_help(cls):
        """Get help text for command line options"""
        return cls._parser.format_help()

    @classmethod
    def from_args(cls, argv: List[str]) -> "CogOptions":
        try:
            args = cls._parser.parse_args(argv)
            return cls(**args.__dict__)
        except argparse.ArgumentError as err:
            raise CogUsageError(str(err))

    def __post_init__(self):
        if self.replace and self.delete_code:
            raise CogUsageError(
                "Can't use -d with -r (or you would delete all your source!)"
            )

        if self.replace and self.output_name:
            raise CogUsageError("Can't use -o with -r (they are opposites)")

        if self.output_name and any(f.filelist_type for f in self.inputs):
            type = next((f.filelist_type for f in self.inputs if f.filelist_type), None)
            raise CogUsageError(f"Can't use -o with {type}file")

        if self.diff and not self.check:
            raise CogUsageError("Can't use --diff without --check")

    def resolved_inputs(self) -> Iterator[CogResolvedInput]:
        """
        Get concrete files to process for cog snippets, paired with their options. Resolves FILELIST
        arguments by reading & parsing their lines options.

        Be sure to `os.chdir` into the value of `CogOptions.chdir` (if it's not `.`) before
        processing the resolved inputs.
        """
        for input in self.inputs:
            yield from self._resolve_input(input)

    def _resolve_input(self, input: CogInput) -> Iterator[CogResolvedInput]:
        match input:
            case CogInput(filelist, FileListType.PLAIN):
                yield from self._resolve_filelist(filelist)
            case CogInput(filelist, FileListType.CHDIR):
                dir, name = os.path.split(filelist)
                from_dir = replace(self, chdir=self.chdir / dir)
                yield from from_dir._resolve_input(CogInput(name, FileListType.PLAIN))
            case CogInput("-"):
                yield CogResolvedInput("-", self)
            case CogInput(filestr):
                files = glob.glob(filestr, root_dir=self.chdir) or [filestr]
                for file in files:
                    dir = os.path.dirname(file)
                    with_file_dir_included = replace(
                        self, include_path=self.include_path + [dir]
                    )
                    yield CogResolvedInput(file, with_file_dir_included)

    def _resolve_filelist(self, filelist: str) -> Iterator[CogResolvedInput]:
        curopts_empty_input = replace(self, inputs=[])
        with open(filelist, encoding=self.encoding) as filelist_in:
            for line in filelist_in:
                argv = _lex_filelist_line(line)
                if argv:
                    args = self._parser.parse_args(argv)
                    line_opts = replace(curopts_empty_input, **args.__dict__)
                    yield from line_opts.resolved_inputs()


def _lex_filelist_line(line: str) -> list[str]:
    # Use shlex to parse the line like a shell.
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    # No escapes, so that backslash can be part of the path
    lex.escape = ""
    return list(lex)
