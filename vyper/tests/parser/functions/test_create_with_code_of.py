

def test_create_forwarder_to_create(get_contract):
    code = """
main: address

@public
def test() -> address:
    self.main = create_forwarder_to(self)
    return self.main
    """

    c = get_contract(code)

    assert c.test() == '0x4F9DA333DCf4E5A53772791B95c161B2FC041859'


def test_create_forwarder_to_call(get_contract, w3):
    code = """

contract SubContract:

    def hello() -> bytes[100]: constant


other: public(address)


@public
def test() -> address:
    self.other = create_forwarder_to(self)
    return self.other


@public
def hello() -> bytes[100]:
    return b"hello world!"


@public
def test2() -> bytes[100]:
    return SubContract(self.other).hello()

    """

    c = get_contract(code)

    assert c.hello() == b'hello world!'
    c.test(transact={})
    assert c.test2() == b'hello world!'


def test_create_with_code_exception(w3, get_contract, assert_tx_failed):
    code = """

contract SubContract:

    def hello(a: uint256) -> bytes[100]: constant


other: public(address)


@public
def test() -> address:
    self.other = create_forwarder_to(self)
    return self.other


@public
def hello(a: uint256) -> bytes[100]:
    assert a > 0, "invaliddddd"
    return b"hello world!"


@public
def test2(a: uint256) -> bytes[100]:
    return SubContract(self.other).hello(a)
    """

    c = get_contract(code)

    assert c.hello(1) == b'hello world!'
    c.test(transact={})
    assert c.test2(1) == b'hello world!'

    assert_tx_failed(lambda: c.test2(0))

    GAS_SENT = 30000
    tx_hash = c.test2(0, transact={'gas': GAS_SENT})

    receipt = w3.eth.getTransactionReceipt(tx_hash)

    assert receipt['status'] == 0
    assert receipt['gasUsed'] < GAS_SENT
