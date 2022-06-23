import datetime
import logging
import random
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from json import dumps, loads
from secrets import compare_digest
from typing import Union

from jwcrypto.jws import JWS
from jwcrypto.jwk import JWK

from sd_jwt.walk import by_structure as walk_by_structure
from sd_jwt.demo_settings import DEFAULT_SIGNIGN_ALG, SD_CLAIMS_KEY

DEFAULT_EXP_MINS = 15
logger = logging.getLogger(__name__)


# The salts will be selected by the server, of course.
def generate_salt():
    return (
        urlsafe_b64encode(bytes(random.getrandbits(8) for _ in range(16)))
        .decode("ascii")
        .strip("=")
    )


def hash_raw(raw):
    # Calculate the SHA 256 hash and output it base64 encoded
    return urlsafe_b64encode(sha256(raw).digest()).decode("ascii").strip("=")


def hash_claim(salt, value, return_raw=False):
    raw = dumps([salt, value])
    if return_raw:
        return raw
    # Calculate the SHA 256 hash and output it base64 encoded
    return hash_raw(raw.encode("utf-8"))


def _create_sd_claim_entry(key, value: str, salt: str) -> str:
    """
    returns the hashed and salted value string
    key arg is not used here, it's just for compliances to other calls
    """
    return hash_claim(salt, value)


def _create_svc_entry(key, value: str, salt: str) -> str:
    """
    returns a string representation of a list
       [hashed and salted value string, value string]
    key arg is not used here, it's just for compliances to other calls
    """
    return hash_claim(salt, value, return_raw=True)


def create_sd_jwt_and_svc(
    user_claims: dict, issuer: str, issuer_key, holder_key, claim_structure: dict = {},
    iat: Union[int, None] = None, exp: Union[int, None] = None
):
    """
    Create the SD-JWT
    """
    # something like: {'sub': 'zyZQuxk2AUv5_Z_RAMxh9Q', 'given_name': 'EpCuoArhQK6MjmO6D-Bi6w' ...
    salts = walk_by_structure(
        claim_structure, user_claims, lambda _, __, ___=None: generate_salt()
    )

    _iat = iat or int(datetime.datetime.utcnow().timestamp())
    _exp = exp or _iat + (DEFAULT_EXP_MINS * 60)

    # Create the JWS payload
    sd_jwt_payload = {
        "iss": issuer,
        "sub_jwk": holder_key.export_public(as_dict=True),
        "iat": _iat,
        "exp": _exp,
        SD_CLAIMS_KEY: walk_by_structure(salts, user_claims, _create_sd_claim_entry),
    }

    # Sign the SD-JWT using the issuer's key
    sd_jwt = JWS(payload=dumps(sd_jwt_payload))
    sd_jwt.add_signature(
        issuer_key,
        alg=DEFAULT_SIGNIGN_ALG,
        protected=dumps({"alg": DEFAULT_SIGNIGN_ALG}),
    )
    serialized_sd_jwt = sd_jwt.serialize(compact=True)

    # Create the SVC
    svc_payload = {
        SD_CLAIMS_KEY: walk_by_structure(salts, user_claims, _create_svc_entry),
        # "sub_jwk_private": issuer_key.export_private(as_dict=True),
    }
    serialized_svc = (
        urlsafe_b64encode(dumps(svc_payload, indent=4).encode("utf-8"))
        .decode("ascii")
        .strip("=")
    )

    # Return the JWS
    return sd_jwt_payload, serialized_sd_jwt, svc_payload, serialized_svc


def create_release_jwt(nonce, aud, disclosed_claims, serialized_svc, holder_key):
    # Reconstruct hash raw values (salt+claim value) from serialized_svc

    hash_raw_values = loads(urlsafe_b64decode(
        serialized_svc + "=="))[SD_CLAIMS_KEY]

    sd_jwt_release_payload = {
        "nonce": nonce,
        "aud": aud,
        SD_CLAIMS_KEY: walk_by_structure(
            hash_raw_values, disclosed_claims, lambda _, __, raw: raw
        ),
    }

    # Sign the SD-JWT-Release using the holder's key
    sd_jwt_release = JWS(payload=dumps(sd_jwt_release_payload))
    sd_jwt_release.add_signature(
        holder_key,
        alg=DEFAULT_SIGNIGN_ALG,
        protected=dumps({"alg": DEFAULT_SIGNIGN_ALG}),
    )
    serialized_sd_jwt_release = sd_jwt_release.serialize(compact=True)

    return sd_jwt_release_payload, serialized_sd_jwt_release


def _verify_sd_jwt(sd_jwt, issuer_public_key, expected_issuer):
    parsed_input_sd_jwt = JWS()
    parsed_input_sd_jwt.deserialize(sd_jwt)
    parsed_input_sd_jwt.verify(issuer_public_key, alg=DEFAULT_SIGNIGN_ALG)

    sd_jwt_payload = loads(parsed_input_sd_jwt.payload)
    if sd_jwt_payload["iss"] != expected_issuer:
        raise ValueError("Invalid issuer")

    # TODO: Check exp/nbf/iat

    if SD_CLAIMS_KEY not in sd_jwt_payload:
        raise ValueError("No selective disclosure claims in SD-JWT")

    holder_public_key_payload = None
    if "sub_jwk" in sd_jwt_payload:
        holder_public_key_payload = sd_jwt_payload["sub_jwk"]

    return sd_jwt_payload[SD_CLAIMS_KEY], holder_public_key_payload


def _verify_sd_jwt_release(
    sd_jwt_release,
    holder_public_key=None,
    expected_aud=None,
    expected_nonce=None,
    holder_public_key_payload=None,
):
    parsed_input_sd_jwt_release = JWS()
    parsed_input_sd_jwt_release.deserialize(sd_jwt_release)
    if holder_public_key and holder_public_key_payload:
        pubkey = JWK.from_json(dumps(holder_public_key_payload))
        # Because of weird bug of failed != between two public keys
        if not holder_public_key == pubkey:
            raise ValueError("sub_jwk is not matching with HOLDER Public Key.")
    if holder_public_key:
        parsed_input_sd_jwt_release.verify(
            holder_public_key, alg=DEFAULT_SIGNIGN_ALG)

    sd_jwt_release_payload = loads(parsed_input_sd_jwt_release.payload)

    if holder_public_key:
        if sd_jwt_release_payload["aud"] != expected_aud:
            raise ValueError("Invalid audience")
        if sd_jwt_release_payload["nonce"] != expected_nonce:
            raise ValueError("Invalid nonce")

    if SD_CLAIMS_KEY not in sd_jwt_release_payload:
        raise ValueError("No selective disclosure claims in SD-JWT-Release")

    return sd_jwt_release_payload[SD_CLAIMS_KEY]


def _check_claim(claim_name, released_value, sd_jwt_claim_value):
    # the hash of the release claim value must match the claim value in the sd_jwt
    hashed_release_value = hash_raw(released_value.encode("utf-8"))
    if not compare_digest(hashed_release_value, sd_jwt_claim_value):
        raise ValueError(
            "Claim release value does not match the claim value in the SD-JWT"
        )

    decoded = loads(released_value)
    if not isinstance(decoded, list):
        raise ValueError("Claim release value is not a list")

    if len(decoded) != 2:
        raise ValueError("Claim release value is not of length 2")

    return decoded[1]


def verify(
    combined_presentation,
    issuer_public_key,
    expected_issuer,
    holder_public_key=None,
    expected_aud=None,
    expected_nonce=None,
):
    if holder_public_key and (not expected_aud or not expected_nonce):
        raise ValueError(
            "When holder binding is to be checked, aud and nonce need to be provided."
        )

    parts = combined_presentation.split(".")
    if len(parts) != 6:
        raise ValueError(
            "Invalid number of parts in the combined presentation")

    # Verify the SD-JWT
    input_sd_jwt = ".".join(parts[:3])
    sd_jwt_claims, holder_public_key_payload = _verify_sd_jwt(
        input_sd_jwt, issuer_public_key, expected_issuer
    )

    # Verify the SD-JWT-Release
    input_sd_jwt_release = ".".join(parts[3:])
    sd_jwt_release_claims = _verify_sd_jwt_release(
        input_sd_jwt_release,
        holder_public_key,
        expected_aud,
        expected_nonce,
        holder_public_key_payload,
    )

    return walk_by_structure(sd_jwt_claims, sd_jwt_release_claims, _check_claim)
