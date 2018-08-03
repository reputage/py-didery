import libnacl

from ..diderying import ValidationError

from collections import OrderedDict as ODict
try:
    import simplejson as json
except ImportError:
    import json

from ioflo.aid import odict
from ioflo.aio.http import Patron
from ioflo.aio import WireLog
from ioflo.base import Store
from ioflo.aid import timing
from ioflo.aid import getConsole

from ..lib import generating as gen

console = getConsole()


def genKeys():
    seed = libnacl.randombytes(libnacl.crypto_sign_SEEDBYTES)
    vk, sk = libnacl.crypto_sign_seed_keypair(seed)
    did = gen.didGen(vk)

    return gen.keyToKey64u(vk), gen.keyToKey64u(sk), did


def validateDid(did, method="dad"):
    """
    Parses and returns did index keystr from signer key indexed did
    as tuple (did, index, keystr)
    raises ValueError if fails parsing
    """
    try:  # correct did format  pre:method:keystr
        pre, meth, keystr = did.split(":")
    except ValueError as ex:
        raise ValueError("Malformed DID value")

    if pre != "did" or meth != method:
        raise ValueError("Invalid DID value")

    return did, keystr


def verify(sig, msg, vk):
    """
    Returns True if signature sig of message msg is verified with
    verification key vk Otherwise False
    All of sig, msg, vk are bytes
    """
    try:
        result = libnacl.crypto_sign_open(sig + msg, vk)
    except Exception as ex:
        return False
    return True if result else False


def verify64u(signature, message, verkey):
    """
    Returns True if signature is valid for message with respect to verification
    key verkey

    signature and verkey are encoded as unicode base64 url-file strings
    and message is unicode string as would be the case for a json object

    """
    sig = gen.key64uToKey(signature)
    vk = gen.key64uToKey(verkey)

    return verify(sig, message, vk)


def parseJsonFile(file, requireds=()):
    """
        Returns deserialized version of data string if data is correctly formed.
        Otherwise returns None

        :param file is json encoded unicode string
        :param requireds tuple of string keys required in json data
        """
    data = None

    with open(file) as f:
        try:
            # now validate message data
            try:
                data = json.load(f, object_pairs_hook=ODict)
            except ValueError as ex:
                raise ValidationError("Invalid JSON")  # invalid json

            if not data:  # registration must not be empty
                raise ValidationError("Empty body")

            if not isinstance(data, dict):  # must be dict subclass
                raise ValidationError("JSON not dict")

            for field in requireds:
                if field not in data:
                    raise ValidationError("Missing required field {}".format(field))

        except ValidationError:
            raise

        except Exception as ex:  # unknown problem
            print(ex)
            raise ValidationError("Unexpected error")

    return data


def parseConfigFile(file):
    """
    Validate the data in the configuration file
    :param file: click.Path object
    :return: parsed configuration data
    """
    data = parseJsonFile(file, ["servers"])

    if not isinstance(data["servers"], list):
        raise ValidationError('"servers" field must be a list')

    return data


def parseDataFile(file, dtype):
    data = {}

    if dtype == "history":
        data = parseJsonFile(file, ["history"])["history"]

        for field in ["id", "signer", "signers"]:
            if field not in data:
                raise ValidationError("Missing required field {}".format(field))

    if dtype == "otp":
        data = parseJsonFile(file, ["otp"])["otp"]

        for field in ["id", "blob"]:
            if field not in data:
                raise ValidationError("Missing required field {}".format(field))

    # Check for valid did
    validateDid(data["id"])

    return data


def httpRequest(method=u'GET',
                scheme=u'',  # default if not in path
                host=u'localhost',  # default if not in path
                port=None,  # default if not in path
                path=u'/',
                qargs=None,
                headers=None,
                body=b'',
                data=None,
                store=None,
                timeout=10000.0,
                buffer=False,):
    """
    Perform Async ReST request to Backend Server

    Parameters:

    Usage: (Inside a generator function)

        response = yield from backendRequest()

    response is the response if valid else None
    before response is completed the yield from yields up an empty string ''
    once completed then response has a value

    path can be full url with host port etc  path takes precedence over others


    """
    store = store if store is not None else Store(stamp=0.0)
    if buffer:
        wlog = WireLog(buffify=buffer, same=True)
        wlog.reopen()
    else:
        wlog = None

    if headers is None:
        headers = odict([('Accept', 'application/json'),
                         ('Connection', 'close')])

    client = Patron(bufsize=131072,
                    wlog=wlog,
                    store=store,
                    scheme=scheme,
                    hostname=host,
                    port=port,
                    method=method,
                    path=path,
                    qargs=qargs,
                    headers=headers,
                    body=body,
                    data=data,
                    reconnectable=False,
                    )

    console.concise("Making Request {0} {1} ...\n".format(method, path))

    client.transmit()
    # assumes store clock is advanced elsewhere
    timer = timing.StoreTimer(store=store, duration=timeout)
    while ((client.requests or client.connector.txes or not client.responses)
           and not timer.expired):
        try:
            client.serviceAll()
        except Exception as ex:
            console.terse("Error: Servicing client. '{0}'\n".format(ex))
            raise ex
        yield b''  # this is eventually yielded by wsgi app while waiting
        store.advanceStamp(0.125)

    response = None  # in case timed out
    if client.responses:
        response = client.responses.popleft()
    client.close()
    if wlog:
        wlog.close()

    # response.get('status')
    # response.get('body').decode('utf-8')
    return response


def awaitAsync(generators):
    values = {}

    while True:
        remove = []
        for i, generator in generators.items():
            try:
                next(generator)
            except StopIteration as si:
                if si.value:
                    values[i] = (json.loads(si.value[0]), si.value[1])
                else:
                    values[i] = ({"error": "request timeout"}, 0)

                remove.append(i)

        for val in remove:
            generators.pop(val)

        if len(generators) == 0:
            return values
