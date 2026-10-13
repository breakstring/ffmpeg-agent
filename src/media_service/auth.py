import hmac

from starlette.datastructures import Headers

from media_service.config import Settings
from media_service.errors import ServiceError


def authorize(settings: Settings, headers: Headers) -> str:
    simple = "service_token" in settings.model_fields_set
    if simple and settings.service_token is None:
        assert settings.service_caller is not None
        return settings.service_caller
    values = headers.getlist("authorization")
    if len(values) != 1:
        raise ServiceError("UNAUTHORIZED", 401)
    scheme, separator, value = values[0].partition(" ")
    if scheme.lower() != "bearer" or not separator or not value or not value.isascii():
        raise ServiceError("UNAUTHORIZED", 401)
    supplied = value.encode("ascii")
    if simple:
        assert settings.service_token is not None and settings.service_caller is not None
        if hmac.compare_digest(supplied, settings.service_token.get_secret_value().encode("ascii")):
            return settings.service_caller
    else:
        for name, account in settings.callers.items():
            for token in account.tokens:
                if hmac.compare_digest(supplied, token.get_secret_value().encode("ascii")):
                    return name
    raise ServiceError("UNAUTHORIZED", 401)
