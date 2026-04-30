"""
drm_keys.py — Local replacement for the dead sainibotsdrm.vercel.app API

What this does (same as the old API):
  1. Fetches the MPD manifest from ClassPlus DRM URL
  2. Extracts the PSSH (Widevine protection header)
  3. Calls the ClassPlus Widevine license server with your cptoken
  4. Returns { "url": mpd_url, "keys": ["kid:key", ...] }

Requirements:
    pip install pywidevine requests xmltodict

pywidevine provides the Device/PSSH/Cdm classes needed to
talk to the Widevine license server locally without any external API.
"""

import re
import base64
import requests
import xmltodict

# pywidevine — local Widevine CDM (Content Decryption Module)
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ─── ClassPlus Widevine License Server ────────────────────────────────────────
CLASSPLUS_LICENSE_URL = "https://widevine.classplusapp.com/widevine"

# ─── Headers used to talk to ClassPlus APIs ───────────────────────────────────
def _cp_headers(cptoken: str) -> dict:
    return {
        'host': 'api.classplusapp.com',
        'x-access-token': cptoken,
        'accept-language': 'EN',
        'api-version': '18',
        'app-version': '1.4.73.2',
        'build-number': '35',
        'connection': 'Keep-Alive',
        'content-type': 'application/json',
        'device-details': 'Xiaomi_Redmi 7_SDK-32',
        'device-id': 'c28d3cb16bbdac01',
        'region': 'IN',
        'user-agent': 'Mobile-Android',
        'webengage-luid': '00000187-6fe4-5d41-a530-26186858be4c',
        'accept-encoding': 'gzip',
    }


def _get_mpd_url(drm_url: str, cptoken: str) -> str:
    """
    Convert a ClassPlus DRM playlist URL into a signed MPD URL.
    ClassPlus DRM URLs look like:
      https://media-cdn.classplusapp.com/drm/<id>/playlist.m3u8
    We sign them via the ClassPlus API to get the real MPD.
    """
    params = {"url": drm_url}
    resp = requests.get(
        'https://api.classplusapp.com/cams/uploader/video/jw-signed-url',
        headers=_cp_headers(cptoken),
        params=params,
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    signed_url = data.get('url')
    if not signed_url:
        raise Exception(f"ClassPlus API did not return a signed URL. Response: {data}")
    # Convert m3u8 to mpd if needed
    signed_url = signed_url.replace('.m3u8', '.mpd')
    return signed_url


def _extract_pssh_from_mpd(mpd_url: str) -> str:
    """
    Fetch the MPD manifest and extract the Widevine PSSH box (base64).
    The PSSH is inside <ContentProtection> with schemeIdUri for Widevine.
    """
    resp = requests.get(mpd_url, timeout=15)
    resp.raise_for_status()
    mpd_text = resp.text

    # Try regex first — faster than XML parsing
    # Widevine system ID: edef8ba9-79d6-4ace-a3c8-27dcd51d21ed
    pssh_match = re.search(
        r'<cenc:pssh[^>]*>([A-Za-z0-9+/=]+)</cenc:pssh>',
        mpd_text
    )
    if pssh_match:
        return pssh_match.group(1)

    # Fallback: parse XML
    try:
        mpd_dict = xmltodict.parse(mpd_text)
        periods = mpd_dict.get('MPD', {}).get('Period', {})
        if isinstance(periods, dict):
            periods = [periods]
        for period in periods:
            adapt_sets = period.get('AdaptationSet', [])
            if isinstance(adapt_sets, dict):
                adapt_sets = [adapt_sets]
            for adapt in adapt_sets:
                protections = adapt.get('ContentProtection', [])
                if isinstance(protections, dict):
                    protections = [protections]
                for prot in protections:
                    scheme = prot.get('@schemeIdUri', '').lower()
                    if 'edef8ba9' in scheme or 'widevine' in scheme.lower():
                        pssh = prot.get('cenc:pssh') or prot.get('pssh')
                        if pssh:
                            return pssh
    except Exception:
        pass

    raise Exception("Could not extract Widevine PSSH from MPD manifest.")


def _get_widevine_keys(pssh_b64: str, mpd_url: str, cptoken: str, wvd_path: str) -> list:
    """
    Use pywidevine + a .wvd device file to get decryption keys
    from the ClassPlus Widevine license server.

    Returns a list of strings like ["kid1:key1", "kid2:key2"]
    """
    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    pssh = PSSH(pssh_b64)
    challenge = cdm.get_license_challenge(session_id, pssh)

    # Send challenge to ClassPlus license server
    license_resp = requests.post(
        CLASSPLUS_LICENSE_URL,
        headers={
            'x-access-token': cptoken,
            'content-type': 'application/octet-stream',
            'user-agent': 'Mozilla/5.0',
        },
        data=challenge,
        timeout=15
    )
    license_resp.raise_for_status()

    cdm.parse_license(session_id, license_resp.content)
    keys = []
    for key in cdm.get_keys(session_id):
        if key.type == 'CONTENT':
            keys.append(f"{key.kid.hex}:{key.key.hex()}")

    cdm.close(session_id)

    if not keys:
        raise Exception("Widevine license server returned no CONTENT keys.")
    return keys


# ─── Main function — drop-in replacement for the dead API ────────────────────
def get_drm_keys(drm_url: str, cptoken: str, wvd_path: str = "device.wvd") -> dict:
    """
    Full replacement for:
      GET https://sainibotsdrm.vercel.app/api?url={url}&token={cptoken}&auth=...

    Returns:
      { "url": "<signed_mpd_url>", "keys": ["kid:key", ...] }
    or raises Exception with error message.

    Args:
      drm_url  : The classplusapp.com/drm/... URL from your .txt file
      cptoken  : Your ClassPlus JWT token
      wvd_path : Path to your Widevine .wvd device file
    """
    # Step 1: Get signed MPD URL from ClassPlus API
    mpd_url = _get_mpd_url(drm_url, cptoken)

    # Step 2: Extract PSSH from MPD manifest
    pssh_b64 = _extract_pssh_from_mpd(mpd_url)

    # Step 3: Get Widevine decryption keys
    keys = _get_widevine_keys(pssh_b64, mpd_url, cptoken, wvd_path)

    return {
        "url": mpd_url,
        "keys": keys
    }
