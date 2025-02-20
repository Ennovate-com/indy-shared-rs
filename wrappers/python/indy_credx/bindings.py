"""Low-level interaction with the indy-credx library."""

import json
import logging
import os
import sys
from ctypes import (
    Array,
    CDLL,
    POINTER,
    Structure,
    addressof,
    byref,
    cast,
    c_char,
    c_char_p,
    c_int8,
    c_int64,
    c_size_t,
    c_ubyte,
    pointer,
)
from ctypes.util import find_library
from io import BytesIO
from typing import Callable, Optional, Mapping, Sequence, Tuple, Union
from weakref import finalize

from .error import CredxError, CredxErrorCode


CALLBACKS = {}
LIB: CDLL = None
LOGGER = logging.getLogger(__name__)


JsonType = Union[dict, str, bytes, memoryview]


def _struct_dtor(ctype: type, address: int, dtor: Callable):
    value = ctype.from_address(address)
    if value:
        dtor(value)


def finalize_struct(instance, ctype):
    """Attach a struct destructor."""
    finalize(
        instance, _struct_dtor, ctype, addressof(instance), instance.__class__._cleanup
    )


def keepalive(instance, *depend):
    """Ensure that dependencies are kept alive as long as the instance."""
    finalize(instance, lambda *_args: None, *depend)


class ObjectHandle(Structure):
    """Index of an active IndyObject instance."""

    _fields_ = [
        ("value", c_int64),
    ]

    def __init__(self, value=0):
        """Initializer."""
        if isinstance(value, c_int64):
            value = value.value
        if not isinstance(value, int):
            raise ValueError("Invalid handle")
        super().__init__(value=value)
        finalize_struct(self, c_int64)

    @property
    def type_name(self) -> str:
        return object_get_type_name(self)

    def __repr__(self) -> str:
        """Format object handle as a string."""
        if self.value:
            try:
                type_name = f'"{self.type_name}"'
            except CredxError:
                type_name = "<error>"
        else:
            type_name = "<none>"
        return f"{self.__class__.__name__}({type_name}, {self.value})"

    @classmethod
    def _cleanup(cls, value: c_int64):
        """Destructor."""
        get_library().credx_object_free(value)


class IndyObject:
    """A generic Indy object allocated by the library."""

    def __init__(self, handle: ObjectHandle) -> "IndyObject":
        self.handle = handle

    def __bytes__(self) -> bytes:
        return bytes(self.to_json_buffer())

    def __repr__(self) -> str:
        """Format object as a string."""
        return f"{self.__class__.__name__}({self.handle.value})"

    def copy(self):
        return self.__class__(self.handle)

    def to_dict(self) -> dict:
        return json.load(BytesIO(self.to_json_buffer()))

    def to_json(self) -> str:
        return bytes(object_get_json(self.handle)).decode("utf-8")

    def to_json_buffer(self) -> memoryview:
        return object_get_json(self.handle).raw


class RawBuffer(Structure):
    """A byte buffer allocated by the library."""

    _fields_ = [
        ("len", c_int64),
        ("data", POINTER(c_ubyte)),
    ]

    def __bool__(self) -> bool:
        return bool(self.data)

    def __bytes__(self) -> bytes:
        if not self.len:
            return b""
        return bytes(self.array)

    def __len__(self) -> int:
        return int(self.len)

    @property
    def array(self) -> Array:
        return cast(self.data, POINTER(c_ubyte * self.len)).contents

    def __repr__(self) -> str:
        return f"<RawBuffer(len={self.len})>"


class ByteBuffer(Structure):
    """A managed byte buffer allocated by the library."""

    _fields_ = [("buffer", RawBuffer)]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        finalize_struct(self, RawBuffer)

    @property
    def _as_parameter_(self):
        return self.buffer

    @property
    def array(self) -> Array:
        return self.buffer.array

    @property
    def raw(self) -> memoryview:
        m = memoryview(self.array)
        keepalive(m, self)
        return m

    def __bytes__(self) -> bytes:
        return bytes(self.buffer)

    def __len__(self) -> int:
        return len(self.buffer)

    def __getitem__(self, idx) -> bytes:
        return bytes(self.buffer.array[idx])

    def __repr__(self) -> str:
        """Format byte buffer as a string."""
        return f"{self.__class__.__name__}({bytes(self)})"

    @classmethod
    def _cleanup(cls, buffer: RawBuffer):
        """Call the byte buffer destructor when this instance is released."""
        get_library().credx_buffer_free(buffer)


class StrBuffer(Structure):
    """A string allocated by the library."""

    _fields_ = [("buffer", POINTER(c_char))]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        finalize_struct(self, c_char_p)

    def is_none(self) -> bool:
        """Check if the returned string pointer is null."""
        return not self.buffer

    def opt_str(self) -> Optional[str]:
        """Convert to an optional string."""
        val = self.value
        return val.decode("utf-8") if val is not None else None

    def __bool__(self) -> bool:
        return bool(self.buffer)

    def __bytes__(self) -> bytes:
        """Convert to bytes."""
        bval = self.value
        return bval if bval is not None else bytes()

    def __str__(self):
        """Convert to a string."""
        # not allowed to return None
        val = self.opt_str()
        return val if val is not None else ""

    @property
    def value(self) -> bytes:
        return cast(self.buffer, c_char_p).value

    @classmethod
    def _cleanup(cls, buffer: c_char_p):
        """Call the string destructor when this instance is released."""
        get_library().credx_string_free(buffer)


class FfiObjectHandleList(Structure):
    _fields_ = [
        ("count", c_size_t),
        ("data", POINTER(ObjectHandle)),
    ]

    @classmethod
    def create(cls, values: Optional[Sequence[ObjectHandle]]) -> "FfiObjectHandleList":
        inst = FfiObjectHandleList()
        if values is not None:
            values = list(values)
            inst.count = len(values)
            inst.data = (ObjectHandle * inst.count)(*values)
        return inst


class FfiIntList(Structure):
    _fields_ = [
        ("count", c_size_t),
        ("data", POINTER(c_int64)),
    ]

    @classmethod
    def create(cls, values: Optional[Sequence[str]]) -> "FfiIntList":
        inst = FfiIntList()
        if values is not None:
            values = [c_int64(v) for v in values]
            inst.count = len(values)
            inst.data = (c_int64 * inst.count)(*values)
        return inst


class FfiStrList(Structure):
    _fields_ = [
        ("count", c_size_t),
        ("data", POINTER(c_char_p)),
    ]

    @classmethod
    def create(cls, values: Optional[Sequence[str]]) -> "FfiStrList":
        inst = FfiStrList()
        if values is not None:
            values = [encode_str(v) for v in values]
            inst.count = len(values)
            inst.data = (c_char_p * inst.count)(*values)
        return inst


class CredentialEntry(Structure):
    _fields_ = [
        ("credential", ObjectHandle),
        ("timestamp", c_int64),
        ("rev_state", ObjectHandle),
    ]

    @classmethod
    def create(
        cls,
        credential: IndyObject,
        timestamp: int = None,
        rev_state: IndyObject = None,
    ) -> "CredentialEntry":
        entry = CredentialEntry(
            credential=credential.handle,
            timestamp=-1 if timestamp is None else timestamp,
            rev_state=rev_state.handle if rev_state else ObjectHandle(),
        )
        keepalive(entry, credential, rev_state)
        return entry


class CredentialEntryList(Structure):
    _fields_ = [
        ("count", c_int64),
        ("data", POINTER(CredentialEntry)),
    ]


class CredentialProve(Structure):
    _fields_ = [
        ("entry_idx", c_int64),
        ("referent", c_char_p),
        ("is_predicate", c_int8),
        ("reveal", c_int8),
    ]

    @classmethod
    def attribute(
        cls,
        entry_idx: int,
        referent: str,
        reveal: bool,
    ) -> "CredentialProve":
        return CredentialProve(
            entry_idx=entry_idx,
            referent=encode_str(referent),
            is_predicate=False,
            reveal=reveal,
        )

    @classmethod
    def predicate(
        cls,
        entry_idx: int,
        referent: str,
    ) -> "CredentialProve":
        return CredentialProve(
            entry_idx=entry_idx,
            referent=encode_str(referent),
            is_predicate=True,
            reveal=True,
        )


class CredentialProveList(Structure):
    _fields_ = [
        ("count", c_int64),
        ("data", POINTER(CredentialProve)),
    ]


class RevocationConfig(Structure):
    _fields_ = [
        ("rev_reg_def", ObjectHandle),
        ("rev_reg_def_private", ObjectHandle),
        ("rev_reg", ObjectHandle),
        ("rev_reg_index", c_int64),
        ("rev_reg_used", FfiIntList),
    ]

    @classmethod
    def create(
        cls,
        rev_reg_def: IndyObject,
        rev_reg_def_private: IndyObject,
        rev_reg: IndyObject,
        rev_reg_index: int,
        rev_reg_used: Sequence[int],
    ) -> "RevocationConfig":
        config = RevocationConfig(
            rev_reg_def=rev_reg_def.handle,
            rev_reg_def_private=rev_reg_def_private.handle,
            rev_reg=rev_reg.handle,
            rev_reg_index=rev_reg_index,
            rev_reg_used=FfiIntList.create(rev_reg_used),
        )
        keepalive(config, rev_reg_def, rev_reg_def_private, rev_reg)
        return config


class RevocationEntry(Structure):
    _fields_ = [
        ("def_entry_idx", c_int64),
        ("registry", ObjectHandle),
        ("timestamp", c_int64),
    ]

    @classmethod
    def create(
        cls,
        def_entry_idx: int,
        registry: IndyObject,
        timestamp: int,
    ) -> "RevocationEntry":
        entry = RevocationEntry(
            def_entry_idx=def_entry_idx,
            registry=registry.handle,
            timestamp=timestamp,
        )
        keepalive(entry, registry)
        return entry


class RevocationEntryList(Structure):
    _fields_ = [
        ("count", c_int64),
        ("data", POINTER(RevocationEntry)),
    ]


def get_library() -> CDLL:
    """Return the CDLL instance, loading it if necessary."""
    global LIB
    if LIB is None:
        LIB = _load_library("indy_credx")
        do_call("credx_set_default_logger")
    return LIB


def library_version() -> str:
    """Get the version of the installed aries-askar library."""
    lib_fn = getattr(get_library(), "credx_version")
    lib_fn.restype = StrBuffer
    return str(lib_fn())


def _load_library(lib_name: str) -> CDLL:
    """Load the CDLL library.
    The python module directory is searched first, followed by the usual
    library resolution for the current system.
    """
    lib_prefix_mapping = {"win32": ""}
    lib_suffix_mapping = {"darwin": ".dylib", "win32": ".dll"}
    try:
        os_name = sys.platform
        lib_prefix = lib_prefix_mapping.get(os_name, "lib")
        lib_suffix = lib_suffix_mapping.get(os_name, ".so")
        lib_path = os.path.join(
            os.path.dirname(__file__), f"{lib_prefix}{lib_name}{lib_suffix}"
        )
        return CDLL(lib_path)
    except KeyError:
        LOGGER.debug("Unknown platform for shared library")
    except OSError:
        LOGGER.warning("Library not loaded from python package")

    lib_path = find_library(lib_name)
    if not lib_path:
        raise CredxError(
            CredxErrorCode.WRAPPER, f"Library not found in path: {lib_path}"
        )
    try:
        return CDLL(lib_path)
    except OSError as e:
        raise CredxError(
            CredxErrorCode.WRAPPER, f"Error loading library: {lib_path}"
        ) from e


def do_call(fn_name, *args):
    """Perform a synchronous library function call."""
    lib_fn = getattr(get_library(), fn_name)
    lib_fn.restype = c_int64
    result = lib_fn(*args)
    if result:
        raise get_current_error(True)


def get_current_error(expect: bool = False) -> Optional[CredxError]:
    """
    Get the error result from the previous failed API method.

    Args:
        expect: Return a default error message if none is found
    """
    err_json = StrBuffer()
    if not get_library().credx_get_current_error(byref(err_json)):
        try:
            msg = json.loads(err_json.value)
        except json.JSONDecodeError:
            LOGGER.warning("JSON decode error for credx_get_current_error")
            msg = None
        if msg and "message" in msg and "code" in msg:
            return CredxError(
                CredxErrorCode(msg["code"]), msg["message"], msg.get("extra")
            )
        if not expect:
            return None
    return CredxError(CredxErrorCode.WRAPPER, "Unknown error")


def decode_str(value: c_char_p) -> str:
    return value.decode("utf-8")


def encode_str(arg: Optional[Union[str, bytes]]) -> c_char_p:
    """
    Encode an optional input argument as a string.

    Returns: None if the argument is None, otherwise the value encoded utf-8.
    """
    if arg is None:
        return c_char_p()
    if isinstance(arg, str):
        return c_char_p(arg.encode("utf-8"))
    return c_char_p(arg)


class FfiByteBuffer(Structure):
    """A byte buffer allocated by python."""

    _fields_ = [
        ("len", c_int64),
        ("value", POINTER(c_ubyte)),
    ]


def encode_bytes(arg: Optional[Union[str, bytes]]) -> FfiByteBuffer:
    buf = FfiByteBuffer()
    if isinstance(arg, memoryview):
        buf.len = arg.nbytes
        if arg.contiguous and not arg.readonly:
            buf.value = (c_ubyte * buf.len).from_buffer(arg.obj)
        else:
            buf.value = (c_ubyte * buf.len).from_buffer_copy(arg.obj)
    elif isinstance(arg, bytearray):
        buf.len = len(arg)
        buf.value = (c_ubyte * buf.len).from_buffer(arg)
    elif arg is not None:
        if isinstance(arg, str):
            arg = arg.encode("utf-8")
        buf.len = len(arg)
        buf.value = (c_ubyte * buf.len).from_buffer_copy(arg)
    return buf


def object_get_json(handle: ObjectHandle) -> ByteBuffer:
    result = ByteBuffer()
    do_call("credx_object_get_json", handle, byref(result))
    return result


def object_get_type_name(handle: ObjectHandle) -> StrBuffer:
    result = StrBuffer()
    do_call("credx_object_get_type_name", handle, byref(result))
    return result


def _object_from_json(method: str, value: JsonType) -> ObjectHandle:
    if isinstance(value, dict):
        value = json.dumps(value)
    result = ObjectHandle()
    do_call(method, encode_bytes(value), byref(result))
    return result


def _object_get_attribute(
    method: str, handle: ObjectHandle, name: str
) -> Optional[StrBuffer]:
    result = StrBuffer()
    do_call(method, handle, encode_str(name), byref(result))
    if result.is_none():
        result = None
    return result


def generate_nonce() -> str:
    result = StrBuffer()
    do_call("credx_generate_nonce", byref(result))
    return str(result)


def create_schema(
    origin_did: str,
    name: str,
    version: str,
    attr_names: Sequence[str],
    seq_no: int = None,
) -> ObjectHandle:
    result = ObjectHandle()
    attrs = FfiStrList.create(attr_names)
    do_call(
        "credx_create_schema",
        encode_str(origin_did),
        encode_str(name),
        encode_str(version),
        attrs,
        c_int64(seq_no or -1),
        byref(result),
    )
    return result


def create_credential_definition(
    origin_did: str,
    schema: ObjectHandle,
    tag: str,
    signature_type: str,
    support_revocation: bool,
) -> Tuple[ObjectHandle, ObjectHandle, ObjectHandle]:
    cred_def, cred_def_pvt, key_proof = ObjectHandle(), ObjectHandle(), ObjectHandle()
    do_call(
        "credx_create_credential_definition",
        encode_str(origin_did),
        schema,
        encode_str(tag),
        encode_str(signature_type),
        c_int8(support_revocation),
        byref(cred_def),
        byref(cred_def_pvt),
        byref(key_proof),
    )
    return (cred_def, cred_def_pvt, key_proof)


def create_credential(
    cred_def: ObjectHandle,
    cred_def_private: ObjectHandle,
    cred_offer: ObjectHandle,
    cred_request: ObjectHandle,
    attr_raw_values: Mapping[str, str],
    attr_enc_values: Optional[Mapping[str, str]],
    revocation_config: Optional[RevocationConfig],
) -> Tuple[ObjectHandle, ObjectHandle, ObjectHandle]:
    cred = ObjectHandle()
    rev_reg = ObjectHandle()
    rev_delta = ObjectHandle()
    attr_keys = list(attr_raw_values.keys())
    names_list = FfiStrList.create(attr_keys)
    raw_values_list = FfiStrList.create(str(attr_raw_values[k]) for k in attr_keys)
    if attr_enc_values:
        enc_values_list = []
        for name in attr_raw_values:
            enc_values_list.append(attr_enc_values.get(name))
    else:
        enc_values_list = None
    enc_values_list = FfiStrList().create(enc_values_list)
    do_call(
        "credx_create_credential",
        cred_def,
        cred_def_private,
        cred_offer,
        cred_request,
        names_list,
        raw_values_list,
        enc_values_list,
        pointer(revocation_config)
        if revocation_config
        else POINTER(RevocationConfig)(),
        byref(cred),
        byref(rev_reg),
        byref(rev_delta),
    )
    return cred, rev_reg, rev_delta


def encode_credential_attributes(
    attr_raw_values: Mapping[str, str]
) -> Mapping[str, str]:
    attr_keys = list(attr_raw_values.keys())
    raw_values_list = FfiStrList.create(str(attr_raw_values[k]) for k in attr_keys)
    result = StrBuffer()
    do_call("credx_encode_credential_attributes", raw_values_list, byref(result))
    return dict(zip(attr_keys, str(result).split(",")))


def process_credential(
    cred: ObjectHandle,
    cred_req_metadata: ObjectHandle,
    link_secret: ObjectHandle,
    cred_def: ObjectHandle,
    rev_reg_def: Optional[ObjectHandle],
) -> ObjectHandle:
    result = ObjectHandle()
    do_call(
        "credx_process_credential",
        cred,
        cred_req_metadata,
        link_secret,
        cred_def,
        rev_reg_def or ObjectHandle(),
        byref(result),
    )
    return result


def revoke_credential(
    cred_def: ObjectHandle,
    rev_reg_def: ObjectHandle,
    rev_reg_def_private: ObjectHandle,
    rev_reg: ObjectHandle,
    cred_rev_idx: int,
) -> Tuple[ObjectHandle, ObjectHandle]:
    upd_rev_reg = ObjectHandle()
    rev_delta = ObjectHandle()
    do_call(
        "credx_revoke_credential",
        cred_def,
        rev_reg_def,
        rev_reg_def_private,
        rev_reg,
        c_int64(cred_rev_idx),
        byref(upd_rev_reg),
        byref(rev_delta),
    )
    return upd_rev_reg, rev_delta


def create_credential_offer(
    schema_id: str, cred_def: ObjectHandle, key_proof: ObjectHandle
) -> ObjectHandle:
    cred_offer = ObjectHandle()
    do_call(
        "credx_create_credential_offer",
        encode_str(schema_id),
        cred_def,
        key_proof,
        byref(cred_offer),
    )
    return cred_offer


def create_credential_request(
    prover_did: str,
    cred_def: ObjectHandle,
    link_secret: ObjectHandle,
    link_secret_id: str,
    cred_offer: ObjectHandle,
) -> Tuple[ObjectHandle, ObjectHandle]:
    cred_req, cred_req_metadata = ObjectHandle(), ObjectHandle()
    do_call(
        "credx_create_credential_request",
        encode_str(prover_did),
        cred_def,
        link_secret,
        encode_str(link_secret_id),
        cred_offer,
        byref(cred_req),
        byref(cred_req_metadata),
    )
    return (cred_req, cred_req_metadata)


def create_link_secret() -> ObjectHandle:
    secret = ObjectHandle()
    do_call(
        "credx_create_link_secret",
        byref(secret),
    )
    return secret


def create_presentation(
    pres_req: ObjectHandle,
    credentials: Sequence[CredentialEntry],
    credentials_prove: Sequence[CredentialProve],
    self_attest: Mapping[str, str],
    link_secret: ObjectHandle,
    schemas: Sequence[ObjectHandle],
    cred_defs: Sequence[ObjectHandle],
) -> ObjectHandle:
    entry_list = CredentialEntryList()
    entry_list.count = len(credentials)
    entry_list.data = (CredentialEntry * entry_list.count)(*credentials)
    prove_list = CredentialProveList()
    prove_list.count = len(credentials_prove)
    prove_list.data = (CredentialProve * prove_list.count)(*credentials_prove)
    present = ObjectHandle()
    do_call(
        "credx_create_presentation",
        pres_req,
        entry_list,
        prove_list,
        FfiStrList.create(self_attest.keys()),
        FfiStrList.create(self_attest.values()),
        link_secret,
        FfiObjectHandleList.create(schemas),
        FfiObjectHandleList.create(cred_defs),
        byref(present),
    )
    return present


def verify_presentation(
    presentation: ObjectHandle,
    pres_req: ObjectHandle,
    schemas: Sequence[ObjectHandle],
    cred_defs: Sequence[ObjectHandle],
    rev_reg_defs: Sequence[ObjectHandle],
    rev_regs: Sequence[RevocationEntry],
    accept_legacy_revocation: bool = False,
) -> bool:
    verify = c_int8()
    entry_list = RevocationEntryList()
    if rev_regs:
        entry_list.count = len(rev_regs)
        entry_list.data = (RevocationEntry * entry_list.count)(*rev_regs)
    do_call(
        "credx_verify_presentation_legacy" if accept_legacy_revocation else "credx_verify_presentation",
        presentation,
        pres_req,
        FfiObjectHandleList.create(schemas),
        FfiObjectHandleList.create(cred_defs),
        FfiObjectHandleList.create(rev_reg_defs),
        entry_list,
        byref(verify),
    )
    return bool(verify)


def create_revocation_registry(
    origin_did: str,
    cred_def: ObjectHandle,
    tag: str,
    rev_reg_type: str,
    issuance_type: Optional[str],
    max_cred_num: int,
    tails_dir_path: Optional[str],
) -> Tuple[ObjectHandle, ObjectHandle, ObjectHandle, ObjectHandle]:
    reg_def = ObjectHandle()
    reg_def_private = ObjectHandle()
    reg_entry = ObjectHandle()
    reg_init_delta = ObjectHandle()
    do_call(
        "credx_create_revocation_registry",
        encode_str(origin_did),
        cred_def,
        encode_str(tag),
        encode_str(rev_reg_type),
        encode_str(issuance_type),
        c_int64(max_cred_num),
        encode_str(tails_dir_path),
        byref(reg_def),
        byref(reg_def_private),
        byref(reg_entry),
        byref(reg_init_delta),
    )
    return reg_def, reg_def_private, reg_entry, reg_init_delta


def update_revocation_registry(
    cred_def: ObjectHandle,
    rev_reg_def: ObjectHandle,
    rev_reg_def_private: ObjectHandle,
    rev_reg: ObjectHandle,
    issued: Sequence[int],
    revoked: Sequence[int],
) -> Tuple[ObjectHandle, ObjectHandle]:
    upd_rev_reg = ObjectHandle()
    rev_delta = ObjectHandle()
    do_call(
        "credx_update_revocation_registry",
        cred_def,
        rev_reg_def,
        rev_reg_def_private,
        rev_reg,
        FfiIntList.create(issued),
        FfiIntList.create(revoked),
        byref(upd_rev_reg),
        byref(rev_delta),
    )
    return upd_rev_reg, rev_delta


def merge_revocation_registry_deltas(
    rev_reg_delta_1: ObjectHandle,
    rev_reg_delta_2: ObjectHandle,
) -> ObjectHandle:
    rev_delta = ObjectHandle()
    do_call(
        "credx_merge_revocation_registry_deltas",
        rev_reg_delta_1,
        rev_reg_delta_2,
        byref(rev_delta),
    )
    return rev_delta


def create_or_update_revocation_state(
    rev_reg_def: ObjectHandle,
    rev_reg_delta: ObjectHandle,
    rev_reg_index: int,
    timestamp: int,
    tails_path: str,
    prev_rev_state: Optional[ObjectHandle],
) -> ObjectHandle:
    rev_state = ObjectHandle()
    do_call(
        "credx_create_or_update_revocation_state",
        rev_reg_def,
        rev_reg_delta,
        c_int64(rev_reg_index),
        c_int64(timestamp),
        encode_str(tails_path),
        prev_rev_state or ObjectHandle(),
        byref(rev_state),
    )
    return rev_state
