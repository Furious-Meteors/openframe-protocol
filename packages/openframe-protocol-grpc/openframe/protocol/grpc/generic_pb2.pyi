from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class GenericRequest(_message.Message):
    __slots__ = ("json_payload",)
    JSON_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    json_payload: str
    def __init__(self, json_payload: _Optional[str] = ...) -> None: ...

class GenericResponse(_message.Message):
    __slots__ = ("json_payload",)
    JSON_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    json_payload: str
    def __init__(self, json_payload: _Optional[str] = ...) -> None: ...
