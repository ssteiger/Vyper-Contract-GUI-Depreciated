import pytest
from pytest import (
    raises,
)

from vyper import (
    compiler,
)
from vyper.exceptions import (
    ParserException,
    StructureException,
)

fail_list = [
    """
@public
def foo() -> uint256:
    doesnotexist(2, uint256)
    return convert(2, uint256)
    """,
    """
@public
def foo() -> uint256:
    convert(2, uint256)
    return convert(2, uint256)

    """,
    ("""
@private
def test(a : uint256):
    pass


@public
def burn(_value: uint256):
    self.test(msg.sender._value)
    """, ParserException)
]


@pytest.mark.parametrize('bad_code', fail_list)
def test_functions_call_fail(bad_code):

    if isinstance(bad_code, tuple):
        with raises(bad_code[1]):
            compiler.compile_code(bad_code[0])
    else:
        with raises(StructureException):
            compiler.compile_code(bad_code)


valid_list = [
    """
@public
def foo() -> uint256:
    return convert(2, uint256)
    """
]


@pytest.mark.parametrize('good_code', valid_list)
def test_functions_call_success(good_code):
    assert compiler.compile_code(good_code) is not None
