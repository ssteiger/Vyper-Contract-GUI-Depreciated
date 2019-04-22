import ast
import functools
from typing import (
    List,
)

from vyper.exceptions import (
    EventDeclarationException,
    FunctionDeclarationException,
    InvalidLiteralException,
    ParserException,
    StructureException,
    TypeMismatchException,
)
from vyper.parser.context import (
    Constancy,
    Context,
)
from vyper.parser.expr import (
    Expr,
)
from vyper.parser.global_context import (
    GlobalContext,
)
from vyper.parser.lll_node import (
    LLLnode,
)
from vyper.parser.parser_utils import (
    annotate_and_optimize_ast,
    base_type_conversion,
    byte_array_to_num,
    getpos,
    make_byte_array_copier,
    make_setter,
    unwrap_location,
)
from vyper.parser.pre_parser import (
    pre_parse,
)
from vyper.parser.stmt import (
    Stmt,
)
from vyper.signatures import (
    sig_utils,
)
from vyper.signatures.event_signature import (
    EventSignature,
)
from vyper.signatures.function_signature import (
    FunctionSignature,
    VariableRecord,
)
from vyper.signatures.interface import (
    check_valid_contract_interface,
)
from vyper.types import (
    BaseType,
    ByteArrayLike,
    ListType,
    ceil32,
    get_size_of_type,
    is_base_type,
)
from vyper.utils import (
    LOADED_LIMIT_MAP,
    MemoryPositions,
    bytes_to_int,
    calc_mem_gas,
    string_to_bytes,
)

if not hasattr(ast, 'AnnAssign'):
    raise Exception("Requires python 3.6 or higher for annotation support")


def parse_to_ast(source_code: str) -> List[ast.stmt]:
    """
    Parses the given vyper source code and returns a list of python AST objects
    for all statements in the source.  Performs pre-processing of source code
    before parsing as well as post-processing of the resulting AST.

    :param source_code: The vyper source code to be parsed.
    :return: The post-processed list of python AST objects for each statement in
        ``source_code``.
    """
    class_types, reformatted_code = pre_parse(source_code)

    if '\x00' in reformatted_code:
        raise ParserException('No null bytes (\\x00) allowed in the source code.')

    parsed_ast = ast.parse(reformatted_code)
    annotate_and_optimize_ast(parsed_ast, reformatted_code, class_types)

    return parsed_ast.body


# Header code
initializer_list = ['seq', ['mstore', 28, ['calldataload', 0]]]
# Store limit constants at fixed addresses in memory.
initializer_list += [['mstore', pos, limit_size] for pos, limit_size in LOADED_LIMIT_MAP.items()]
initializer_lll = LLLnode.from_list(initializer_list, typ=None)


# Is a function the initializer?
def is_initializer(code):
    return code.name == '__init__'


# Is a function the default function?
def is_default_func(code):
    return code.name == '__default__'


def parse_events(sigs, global_ctx):
    for event in global_ctx._events:
        sigs[event.target.id] = EventSignature.from_declaration(event, global_ctx)
    return sigs


def parse_external_contracts(external_contracts, global_ctx):
    for _contractname in global_ctx._contracts:
        _contract_defs = global_ctx._contracts[_contractname]
        _defnames = [_def.name for _def in _contract_defs]
        contract = {}
        if len(set(_defnames)) < len(_contract_defs):
            raise FunctionDeclarationException(
                "Duplicate function name: %s" % (
                    [name for name in _defnames if _defnames.count(name) > 1][0]
                )
            )

        for _def in _contract_defs:
            constant = False
            # test for valid call type keyword.
            if len(_def.body) == 1 and \
               isinstance(_def.body[0], ast.Expr) and \
               isinstance(_def.body[0].value, ast.Name) and \
               _def.body[0].value.id in ('modifying', 'constant'):
                constant = True if _def.body[0].value.id == 'constant' else False
            else:
                raise StructureException('constant or modifying call type must be specified', _def)
            # Recognizes already-defined structs
            sig = FunctionSignature.from_definition(
                _def,
                contract_def=True,
                constant=constant,
                custom_structs=global_ctx._structs,
                constants=global_ctx._constants
            )
            contract[sig.name] = sig
        external_contracts[_contractname] = contract

    for interface_name, interface in global_ctx._interfaces.items():
        external_contracts[interface_name] = {
            sig.name: sig
            for sig in interface
            if isinstance(sig, FunctionSignature)
        }

    return external_contracts


def parse_other_functions(o,
                          otherfuncs,
                          sigs,
                          external_contracts,
                          origcode,
                          global_ctx,
                          default_function,
                          runtime_only):
    sub = ['seq', initializer_lll]
    add_gas = initializer_lll.gas
    for _def in otherfuncs:
        sub.append(parse_func(_def, {**{'self': sigs}, **external_contracts}, origcode, global_ctx))
        sub[-1].total_gas += add_gas
        add_gas += 30
        for sig in sig_utils.generate_default_arg_sigs(_def, external_contracts, global_ctx):
            sig.gas = sub[-1].total_gas
            sigs[sig.sig] = sig

    # Add fallback function
    if default_function:
        default_func = parse_func(
            default_function[0],
            {**{'self': sigs}, **external_contracts},
            origcode,
            global_ctx,
        )
        sub.append(default_func)
    else:
        sub.append(LLLnode.from_list(['revert', 0, 0], typ=None, annotation='Default function'))
    if runtime_only:
        return sub
    else:
        o.append(['return', 0, ['lll', sub, 0]])
        return o


# Main python parse tree => LLL method
def parse_tree_to_lll(code, origcode, runtime_only=False, interface_codes=None):
    global_ctx = GlobalContext.get_global_context(code, interface_codes=interface_codes)
    _names_def = [_def.name for _def in global_ctx._defs]
    # Checks for duplicate function names
    if len(set(_names_def)) < len(_names_def):
        raise FunctionDeclarationException(
            "Duplicate function name: %s" % (
                [name for name in _names_def if _names_def.count(name) > 1][0]
            )
        )
    _names_events = [_event.target.id for _event in global_ctx._events]
    # Checks for duplicate event names
    if len(set(_names_events)) < len(_names_events):
        raise EventDeclarationException(
            "Duplicate event name: %s" % (
                [name for name in _names_events if _names_events.count(name) > 1][0]
            )
        )
    # Initialization function
    initfunc = [_def for _def in global_ctx._defs if is_initializer(_def)]
    # Default function
    defaultfunc = [_def for _def in global_ctx._defs if is_default_func(_def)]
    # Regular functions
    otherfuncs = [
        _def
        for _def
        in global_ctx._defs
        if not is_initializer(_def) and not is_default_func(_def)
    ]
    sigs = {}
    external_contracts = {}
    # Create the main statement
    o = ['seq']
    if global_ctx._events:
        sigs = parse_events(sigs, global_ctx)
    if global_ctx._contracts or global_ctx._interfaces:
        external_contracts = parse_external_contracts(external_contracts, global_ctx)
    # If there is an init func...
    if initfunc:
        o.append(initializer_lll)
        o.append(parse_func(
            initfunc[0],
            {**{'self': sigs}, **external_contracts},
            origcode,
            global_ctx,
        ))
    # If there are regular functions...
    if otherfuncs or defaultfunc:
        o = parse_other_functions(
            o, otherfuncs, sigs, external_contracts, origcode, global_ctx, defaultfunc, runtime_only
        )

    # Check if interface of contract is correct.
    check_valid_contract_interface(global_ctx, sigs)

    return LLLnode.from_list(o, typ=None)


def _mk_calldatacopy_copier(pos, sz, mempos):
    return ['calldatacopy', mempos, ['add', 4, pos], sz]


def _mk_codecopy_copier(pos, sz, mempos):
    return ['codecopy', mempos, ['add', '~codelen', pos], sz]


# Checks that an input matches its type
def make_clamper(datapos, mempos, typ, is_init=False):
    if not is_init:
        data_decl = ['calldataload', ['add', 4, datapos]]
        copier = functools.partial(_mk_calldatacopy_copier, mempos=mempos)
    else:
        data_decl = ['codeload', ['add', '~codelen', datapos]]
        copier = functools.partial(_mk_codecopy_copier, mempos=mempos)
    # Numbers: make sure they're in range
    if is_base_type(typ, 'int128'):
        return LLLnode.from_list([
            'clamp',
            ['mload', MemoryPositions.MINNUM],
            data_decl,
            ['mload', MemoryPositions.MAXNUM]
        ], typ=typ, annotation='checking int128 input')
    # Booleans: make sure they're zero or one
    elif is_base_type(typ, 'bool'):
        return LLLnode.from_list(
            ['uclamplt', data_decl, 2],
            typ=typ,
            annotation='checking bool input',
        )
    # Addresses: make sure they're in range
    elif is_base_type(typ, 'address'):
        return LLLnode.from_list(
            ['uclamplt', data_decl, ['mload', MemoryPositions.ADDRSIZE]],
            typ=typ,
            annotation='checking address input',
        )
    # Bytes: make sure they have the right size
    elif isinstance(typ, ByteArrayLike):
        return LLLnode.from_list([
            'seq',
            copier(data_decl, 32 + typ.maxlen),
            ['assert', ['le', ['calldataload', ['add', 4, data_decl]], typ.maxlen]]
        ], typ=None, annotation='checking bytearray input')
    # Lists: recurse
    elif isinstance(typ, ListType):
        o = []
        for i in range(typ.count):
            offset = get_size_of_type(typ.subtype) * 32 * i
            o.append(make_clamper(datapos + offset, mempos + offset, typ.subtype, is_init))
        return LLLnode.from_list(['seq'] + o, typ=None, annotation='checking list input')
    # Otherwise don't make any checks
    else:
        return LLLnode.from_list('pass')


def get_sig_statements(sig, pos):
    method_id_node = LLLnode.from_list(sig.method_id, pos=pos, annotation='%s' % sig.sig)

    if sig.private:
        sig_compare = 0
        private_label = LLLnode.from_list(
            ['label', 'priv_{}'.format(sig.method_id)],
            pos=pos, annotation='%s' % sig.sig
        )
    else:
        sig_compare = ['eq', ['mload', 0], method_id_node]
        private_label = ['pass']

    return sig_compare, private_label


def get_arg_copier(sig, total_size, memory_dest, offset=4):
    # Copy arguments.
    # For private function, MSTORE arguments and callback pointer from the stack.
    if sig.private:
        copier = ['seq']
        for pos in range(0, total_size, 32):
            copier.append(['mstore', memory_dest + pos, 'pass'])
    else:
        copier = ['calldatacopy', memory_dest, offset, total_size]

    return copier


def make_unpacker(ident, i_placeholder, begin_pos):
    start_label = 'dyn_unpack_start_' + ident
    end_label = 'dyn_unpack_end_' + ident
    return [
        'seq_unchecked',
        ['mstore', begin_pos, 'pass'],  # get len
        ['mstore', i_placeholder, 0],
        ['label', start_label],
        [  # break
            'if',
            ['ge', ['mload', i_placeholder], ['ceil32', ['mload', begin_pos]]],
            ['goto', end_label],
        ],
        [  # pop into correct memory slot.
            'mstore',
            ['add', ['add', begin_pos, 32], ['mload', i_placeholder]],
            'pass',
        ],
        ['mstore', i_placeholder, ['add', 32, ['mload', i_placeholder]]],  # increment i
        ['goto', start_label],
        ['label', end_label]]


# Parses a function declaration
def parse_func(code, sigs, origcode, global_ctx, _vars=None):
    if _vars is None:
        _vars = {}
    sig = FunctionSignature.from_definition(
        code,
        sigs=sigs,
        custom_units=global_ctx._custom_units,
        custom_structs=global_ctx._structs,
        constants=global_ctx._constants
    )
    # Get base args for function.
    total_default_args = len(code.args.defaults)
    base_args = sig.args[:-total_default_args] if total_default_args > 0 else sig.args
    default_args = code.args.args[-total_default_args:]
    default_values = dict(zip([arg.arg for arg in default_args], code.args.defaults))
    # __init__ function may not have defaults.
    if sig.name == '__init__' and total_default_args > 0:
        raise FunctionDeclarationException("__init__ function may not have default parameters.")
    # Check for duplicate variables with globals
    for arg in sig.args:
        if arg.name in global_ctx._globals:
            raise FunctionDeclarationException(
                "Variable name duplicated between function arguments and globals: " + arg.name
            )

    nonreentrant_pre = [['pass']]
    nonreentrant_post = [['pass']]
    if sig.nonreentrant_key:
        nkey = global_ctx.get_nonrentrant_counter(sig.nonreentrant_key)
        nonreentrant_pre = [
            ['seq',
                ['assert', ['iszero', ['sload', nkey]]],
                ['sstore', nkey, 1]]]
        nonreentrant_post = [['sstore', nkey, 0]]

    # Create a local (per function) context.
    context = Context(
        vars=_vars,
        global_ctx=global_ctx,
        sigs=sigs,
        return_type=sig.output_type,
        constancy=Constancy.Constant if sig.const else Constancy.Mutable,
        is_payable=sig.payable,
        origcode=origcode,
        is_private=sig.private,
        method_id=sig.method_id
    )

    # Copy calldata to memory for fixed-size arguments
    max_copy_size = sum([
        32 if isinstance(arg.typ, ByteArrayLike) else get_size_of_type(arg.typ) * 32
        for arg in sig.args
    ])
    base_copy_size = sum([
        32 if isinstance(arg.typ, ByteArrayLike) else get_size_of_type(arg.typ) * 32
        for arg in base_args
    ])
    context.next_mem += max_copy_size

    clampers = []

    # Create callback_ptr, this stores a destination in the bytecode for a private
    # function to jump to after a function has executed.
    _post_callback_ptr = "{}_{}_post_callback_ptr".format(sig.name, sig.method_id)
    if sig.private:
        context.callback_ptr = context.new_placeholder(typ=BaseType('uint256'))
        clampers.append(
            LLLnode.from_list(
                ['mstore', context.callback_ptr, 'pass'],
                annotation='pop callback pointer',
            )
        )
        if total_default_args > 0:
            clampers.append(['label', _post_callback_ptr])

    # private functions without return types need to jump back to
    # the calling function, as there is no return statement to handle the
    # jump.
    stop_func = [['stop']]
    if sig.output_type is None and sig.private:
        stop_func = [['jump', ['mload', context.callback_ptr]]]

    if not len(base_args):
        copier = 'pass'
    elif sig.name == '__init__':
        copier = ['codecopy', MemoryPositions.RESERVED_MEMORY, '~codelen', base_copy_size]
    else:
        copier = get_arg_copier(
            sig=sig,
            total_size=base_copy_size,
            memory_dest=MemoryPositions.RESERVED_MEMORY
        )
    clampers.append(copier)

    # Add asserts for payable and internal
    # private never gets payable check.
    if not sig.payable and not sig.private:
        clampers.append(['assert', ['iszero', 'callvalue']])

    # Fill variable positions
    for i, arg in enumerate(sig.args):
        if i < len(base_args) and not sig.private:
            clampers.append(make_clamper(
                arg.pos,
                context.next_mem,
                arg.typ,
                sig.name == '__init__',
            ))
        if isinstance(arg.typ, ByteArrayLike):
            context.vars[arg.name] = VariableRecord(arg.name, context.next_mem, arg.typ, False)
            context.next_mem += 32 * get_size_of_type(arg.typ)
        else:
            context.vars[arg.name] = VariableRecord(
                arg.name,
                MemoryPositions.RESERVED_MEMORY + arg.pos,
                arg.typ,
                False,
            )

    # Private function copiers. No clamping for private functions.
    dyn_variable_names = [a.name for a in base_args if isinstance(a.typ, ByteArrayLike)]
    if sig.private and dyn_variable_names:
        i_placeholder = context.new_placeholder(typ=BaseType('uint256'))
        unpackers = []
        for idx, var_name in enumerate(dyn_variable_names):
            var = context.vars[var_name]
            ident = "_load_args_%d_dynarg%d" % (sig.method_id, idx)
            o = make_unpacker(ident=ident, i_placeholder=i_placeholder, begin_pos=var.pos)
            unpackers.append(o)

        if not unpackers:
            unpackers = ['pass']

        clampers.append(LLLnode.from_list(
            # [0] to complete full overarching 'seq' statement, see private_label.
            ['seq_unchecked'] + unpackers + [0],
            typ=None,
            annotation='dynamic unpacker',
            pos=getpos(code),
        ))

    # Create "clampers" (input well-formedness checkers)
    # Return function body
    if sig.name == '__init__':
        o = LLLnode.from_list(
            ['seq'] + clampers + [parse_body(code.body, context)],
            pos=getpos(code),
        )
    elif is_default_func(sig):
        if len(sig.args) > 0:
            raise FunctionDeclarationException(
                'Default function may not receive any arguments.', code
            )
        if sig.private:
            raise FunctionDeclarationException(
                'Default function may only be public.', code,
            )
        o = LLLnode.from_list(
            ['seq'] + clampers + [parse_body(code.body, context)],
            pos=getpos(code),
        )
    else:

        if total_default_args > 0:  # Function with default parameters.
            function_routine = "{}_{}".format(sig.name, sig.method_id)
            default_sigs = sig_utils.generate_default_arg_sigs(code, sigs, global_ctx)
            sig_chain = ['seq']

            for default_sig in default_sigs:
                sig_compare, private_label = get_sig_statements(default_sig, getpos(code))

                # Populate unset default variables
                populate_arg_count = len(sig.args) - len(default_sig.args)
                set_defaults = []
                if populate_arg_count > 0:
                    current_sig_arg_names = {x.name for x in default_sig.args}
                    missing_arg_names = [
                        arg.arg
                        for arg
                        in default_args
                        if arg.arg not in current_sig_arg_names
                    ]
                    for arg_name in missing_arg_names:
                        value = Expr(default_values[arg_name], context).lll_node
                        var = context.vars[arg_name]
                        left = LLLnode.from_list(var.pos, typ=var.typ, location='memory',
                                                 pos=getpos(code), mutable=var.mutable)
                        set_defaults.append(make_setter(left, value, 'memory', pos=getpos(code)))

                current_sig_arg_names = {x.name for x in default_sig.args}
                base_arg_names = {arg.name for arg in base_args}
                if sig.private:
                    # Load all variables in default section, if private,
                    # because the stack is a linear pipe.
                    copier_arg_count = len(default_sig.args)
                    copier_arg_names = current_sig_arg_names
                else:
                    copier_arg_count = len(default_sig.args) - len(base_args)
                    copier_arg_names = current_sig_arg_names - base_arg_names
                # Order copier_arg_names, this is very important.
                copier_arg_names = [x.name for x in default_sig.args if x.name in copier_arg_names]

                # Variables to be populated from calldata/stack.
                default_copiers = []
                if copier_arg_count > 0:
                    # Get map of variables in calldata, with thier offsets
                    offset = 4
                    calldata_offset_map = {}
                    for arg in default_sig.args:
                        calldata_offset_map[arg.name] = offset
                        offset += (
                            32
                            if isinstance(arg.typ, ByteArrayLike)
                            else get_size_of_type(arg.typ) * 32
                        )
                    # Copy set default parameters from calldata
                    dynamics = []
                    for arg_name in copier_arg_names:
                        var = context.vars[arg_name]
                        calldata_offset = calldata_offset_map[arg_name]
                        if sig.private:
                            _offset = calldata_offset
                            if isinstance(var.typ, ByteArrayLike):
                                _size = 32
                                dynamics.append(var.pos)
                            else:
                                _size = var.size * 32
                            default_copiers.append(get_arg_copier(
                                sig=sig,
                                memory_dest=var.pos,
                                total_size=_size,
                                offset=_offset,
                            ))
                        else:
                            # Add clampers.
                            default_copiers.append(make_clamper(
                                calldata_offset - 4,
                                var.pos,
                                var.typ,
                            ))
                            # Add copying code.
                            if isinstance(var.typ, ByteArrayLike):
                                _offset = ['add', 4, ['calldataload', calldata_offset]]
                            else:
                                _offset = calldata_offset
                            default_copiers.append(get_arg_copier(
                                sig=sig,
                                memory_dest=var.pos,
                                total_size=var.size * 32,
                                offset=_offset,
                            ))

                    # Unpack byte array if necessary.
                    if dynamics:
                        i_placeholder = context.new_placeholder(typ=BaseType('uint256'))
                        for idx, var_pos in enumerate(dynamics):
                            ident = 'unpack_default_sig_dyn_%d_arg%d' % (default_sig.method_id, idx)
                            default_copiers.append(make_unpacker(
                                ident=ident,
                                i_placeholder=i_placeholder,
                                begin_pos=var_pos,
                            ))
                    default_copiers.append(0)  # for over arching seq, POP

                sig_chain.append([
                    'if', sig_compare,
                    ['seq',
                        private_label,
                        ['pass'] if not sig.private else LLLnode.from_list([
                            'mstore',
                            context.callback_ptr,
                            'pass',
                        ], annotation='pop callback pointer', pos=getpos(code)),
                        ['seq'] + set_defaults if set_defaults else ['pass'],
                        ['seq_unchecked'] + default_copiers if default_copiers else ['pass'],
                        ['goto', _post_callback_ptr if sig.private else function_routine]]
                ])

            # With private functions all variable loading occurs in the default
            # function sub routine.
            if sig.private:
                _clampers = [['label', _post_callback_ptr]]
            else:
                _clampers = clampers

            # Function with default parameters.
            o = LLLnode.from_list(
                [
                    'seq',
                    sig_chain,
                    [
                        'if', 0,  # can only be jumped into
                        [
                            'seq',
                            ['label', function_routine] if not sig.private else ['pass'],
                            ['seq'] + nonreentrant_pre + _clampers + [
                                parse_body(c, context)
                                for c in code.body
                            ] + nonreentrant_post + stop_func
                        ],
                    ],
                ], typ=None, pos=getpos(code))

        else:
            # Function without default parameters.
            sig_compare, private_label = get_sig_statements(sig, getpos(code))
            o = LLLnode.from_list(
                [
                    'if',
                    sig_compare,
                    ['seq'] + [private_label] + nonreentrant_pre + clampers + [
                        parse_body(c, context)
                        for c
                        in code.body
                    ] + nonreentrant_post + stop_func
                ], typ=None, pos=getpos(code))

    # Check for at leasts one return statement if necessary.
    if context.return_type and context.function_return_count == 0:
        raise FunctionDeclarationException(
            "Missing return statement in function '%s' " % sig.name, code
        )

    o.context = context
    o.total_gas = o.gas + calc_mem_gas(o.context.next_mem)
    o.func_name = sig.name
    return o


# Parse a piece of code
def parse_body(code, context):
    if not isinstance(code, list):
        return parse_stmt(code, context)
    o = []
    for stmt in code:
        lll = parse_stmt(stmt, context)
        o.append(lll)
    return LLLnode.from_list(['seq'] + o, pos=getpos(code[0]) if code else None)


# Parse an expression
def parse_expr(expr, context):
    return Expr(expr, context).lll_node


# Parse a statement (usually one line of code but not always)
def parse_stmt(stmt, context):
    return Stmt(stmt, context).lll_node


def pack_logging_topics(event_id, args, expected_topics, context, pos):
    topics = [event_id]
    code_pos = pos
    for pos, expected_topic in enumerate(expected_topics):
        expected_type = expected_topic.typ
        arg = args[pos]
        value = parse_expr(arg, context)
        arg_type = value.typ

        if isinstance(arg_type, ByteArrayLike) and isinstance(expected_type, ByteArrayLike):
            if arg_type.maxlen > expected_type.maxlen:
                raise TypeMismatchException(
                    "Topic input bytes are too big: %r %r" % (arg_type, expected_type), code_pos
                )
            if isinstance(arg, ast.Str):
                bytez, bytez_length = string_to_bytes(arg.s)
                if len(bytez) > 32:
                    raise InvalidLiteralException(
                        "Can only log a maximum of 32 bytes at a time.", code_pos
                    )
                topics.append(bytes_to_int(bytez + b'\x00' * (32 - bytez_length)))
            else:
                if value.location == "memory":
                    size = ['mload', value]
                elif value.location == "storage":
                    size = ['sload', ['sha3_32', value]]
                topics.append(byte_array_to_num(value, arg, 'uint256', size))
        else:
            value = unwrap_location(value)
            value = base_type_conversion(value, arg_type, expected_type, pos=code_pos)
            topics.append(value)

    return topics


def pack_args_by_32(holder, maxlen, arg, typ, context, placeholder,
                    dynamic_offset_counter=None, datamem_start=None, zero_pad_i=None, pos=None):
    """
    Copy necessary variables to pre-allocated memory section.

    :param holder: Complete holder for all args
    :param maxlen: Total length in bytes of the full arg section (static + dynamic).
    :param arg: Current arg to pack
    :param context: Context of arg
    :param placeholder: Static placeholder for static argument part.
    :param dynamic_offset_counter: position counter stored in static args.
    :param dynamic_placeholder: pointer to current position in memory to write dynamic values to.
    :param datamem_start: position where the whole datemem section starts.
    """

    if isinstance(typ, BaseType):
        if isinstance(arg, LLLnode):
            value = unwrap_location(arg)
        else:
            value = parse_expr(arg, context)
            value = base_type_conversion(value, value.typ, typ, pos)
        holder.append(LLLnode.from_list(['mstore', placeholder, value], typ=typ, location='memory'))
    elif isinstance(typ, ByteArrayLike):

        if isinstance(arg, LLLnode):  # Is prealloacted variable.
            source_lll = arg
        else:
            source_lll = parse_expr(arg, context)

        # Set static offset, in arg slot.
        holder.append(LLLnode.from_list(['mstore', placeholder, ['mload', dynamic_offset_counter]]))
        # Get the biginning to write the ByteArray to.
        dest_placeholder = LLLnode.from_list(
            ['add', datamem_start, ['mload', dynamic_offset_counter]],
            typ=typ, location='memory', annotation="pack_args_by_32:dest_placeholder")
        copier = make_byte_array_copier(dest_placeholder, source_lll, pos=pos)
        holder.append(copier)
        # Add zero padding.
        new_maxlen = ceil32(source_lll.typ.maxlen)

        holder.append([
            'with', '_ceil32_end', ['ceil32', ['mload', dest_placeholder]], [
                'seq', ['with', '_bytearray_loc', dest_placeholder, [
                    'seq', ['repeat', zero_pad_i, ['mload', '_bytearray_loc'], new_maxlen, [
                        'seq',
                        # stay within allocated bounds
                        ['if', ['ge', ['mload', zero_pad_i], '_ceil32_end'], 'break'],
                        [
                            'mstore8',
                            ['add', ['add', '_bytearray_loc', 32], ['mload', zero_pad_i]],
                            0,
                        ],
                    ]],
                ]],
            ]
        ])

        # Increment offset counter.
        increment_counter = LLLnode.from_list([
            'mstore', dynamic_offset_counter,
            [
                'add',
                ['add', ['mload', dynamic_offset_counter], ['ceil32', ['mload', dest_placeholder]]],
                32,
            ],
        ], annotation='Increment dynamic offset counter')
        holder.append(increment_counter)
    elif isinstance(typ, ListType):
        maxlen += (typ.count - 1) * 32
        typ = typ.subtype

        def check_list_type_match(provided):  # Check list types match.
            if provided != typ:
                raise TypeMismatchException(
                    "Log list type '%s' does not match provided, expected '%s'" % (provided, typ)
                )

        # List from storage
        if isinstance(arg, ast.Attribute) and arg.value.id == 'self':
            stor_list = context.globals[arg.attr]
            check_list_type_match(stor_list.typ.subtype)
            size = stor_list.typ.count
            mem_offset = 0
            for i in range(0, size):
                storage_offset = i
                arg2 = LLLnode.from_list(
                    ['sload', ['add', ['sha3_32', Expr(arg, context).lll_node], storage_offset]],
                    typ=typ,
                )
                holder, maxlen = pack_args_by_32(
                    holder,
                    maxlen,
                    arg2,
                    typ,
                    context,
                    placeholder + mem_offset,
                    pos=pos,
                )
                mem_offset += get_size_of_type(typ) * 32

        # List from variable.
        elif isinstance(arg, ast.Name):
            size = context.vars[arg.id].size
            pos = context.vars[arg.id].pos
            check_list_type_match(context.vars[arg.id].typ.subtype)
            mem_offset = 0
            for _ in range(0, size):
                arg2 = LLLnode.from_list(pos + mem_offset, typ=typ, location='memory')
                holder, maxlen = pack_args_by_32(
                    holder,
                    maxlen,
                    arg2,
                    typ,
                    context,
                    placeholder + mem_offset,
                    pos=pos,
                )
                mem_offset += get_size_of_type(typ) * 32

        # List from list literal.
        else:
            mem_offset = 0
            for arg2 in arg.elts:
                holder, maxlen = pack_args_by_32(
                    holder,
                    maxlen,
                    arg2,
                    typ,
                    context,
                    placeholder + mem_offset,
                    pos=pos,
                )
                mem_offset += get_size_of_type(typ) * 32
    return holder, maxlen


# Pack logging data arguments
def pack_logging_data(expected_data, args, context, pos):
    # Checks to see if there's any data
    if not args:
        return ['seq'], 0, None, 0
    holder = ['seq']
    maxlen = len(args) * 32  # total size of all packed args (upper limit)

    # Unroll any function calls, to temp variables.
    prealloacted = {}
    for idx, (arg, _expected_arg) in enumerate(zip(args, expected_data)):

        if isinstance(arg, (ast.Str, ast.Call)):
            expr = Expr(arg, context)
            source_lll = expr.lll_node
            typ = source_lll.typ

            if isinstance(arg, ast.Str):
                if len(arg.s) > typ.maxlen:
                    raise TypeMismatchException(
                        "Data input bytes are to big: %r %r" % (len(arg.s), typ), pos
                    )

            tmp_variable = context.new_variable(
                '_log_pack_var_%i_%i' % (arg.lineno, arg.col_offset),
                source_lll.typ,
            )
            tmp_variable_node = LLLnode.from_list(
                tmp_variable,
                typ=source_lll.typ,
                pos=getpos(arg),
                location="memory",
                annotation='log_prealloacted %r' % source_lll.typ,
            )
            # Store len.
            # holder.append(['mstore', len_placeholder, ['mload', unwrap_location(source_lll)]])
            # Copy bytes.

            holder.append(
                make_setter(tmp_variable_node, source_lll, pos=getpos(arg), location='memory')
            )
            prealloacted[idx] = tmp_variable_node

    requires_dynamic_offset = any([isinstance(data.typ, ByteArrayLike) for data in expected_data])
    if requires_dynamic_offset:
        # Iterator used to zero pad memory.
        zero_pad_i = context.new_placeholder(BaseType('uint256'))
        dynamic_offset_counter = context.new_placeholder(BaseType(32))
        dynamic_placeholder = context.new_placeholder(BaseType(32))
    else:
        dynamic_offset_counter = None
        zero_pad_i = None

    # Create placeholder for static args. Note: order of new_*() is important.
    placeholder_map = {}
    for i, (_arg, data) in enumerate(zip(args, expected_data)):
        typ = data.typ
        if not isinstance(typ, ByteArrayLike):
            placeholder = context.new_placeholder(typ)
        else:
            placeholder = context.new_placeholder(BaseType(32))
        placeholder_map[i] = placeholder

    # Populate static placeholders.
    for i, (arg, data) in enumerate(zip(args, expected_data)):
        typ = data.typ
        placeholder = placeholder_map[i]
        if not isinstance(typ, ByteArrayLike):
            holder, maxlen = pack_args_by_32(
                holder,
                maxlen,
                prealloacted.get(i, arg),
                typ,
                context,
                placeholder,
                zero_pad_i=zero_pad_i,
                pos=pos,
            )

    # Dynamic position starts right after the static args.
    if requires_dynamic_offset:
        holder.append(LLLnode.from_list(['mstore', dynamic_offset_counter, maxlen]))

    # Calculate maximum dynamic offset placeholders, used for gas estimation.
    for _arg, data in zip(args, expected_data):
        typ = data.typ
        if isinstance(typ, ByteArrayLike):
            maxlen += 32 + ceil32(typ.maxlen)

    if requires_dynamic_offset:
        datamem_start = dynamic_placeholder + 32
    else:
        datamem_start = placeholder_map[0]

    # Copy necessary data into allocated dynamic section.
    for i, (arg, data) in enumerate(zip(args, expected_data)):
        typ = data.typ
        if isinstance(typ, ByteArrayLike):
            pack_args_by_32(
                holder=holder,
                maxlen=maxlen,
                arg=prealloacted.get(i, arg),
                typ=typ,
                context=context,
                placeholder=placeholder_map[i],
                datamem_start=datamem_start,
                dynamic_offset_counter=dynamic_offset_counter,
                zero_pad_i=zero_pad_i,
                pos=pos
            )

    return holder, maxlen, dynamic_offset_counter, datamem_start


def parse_to_lll(kode, runtime_only=False, interface_codes=None):
    code = parse_to_ast(kode)
    return parse_tree_to_lll(code, kode, runtime_only=runtime_only, interface_codes=interface_codes)
