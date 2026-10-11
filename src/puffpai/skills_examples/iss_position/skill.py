"""Where the International Space Station is right now.

A hand-written example of the whole skill contract, kept small on purpose: the
consent card shows every line of this file, so a skill that a household cannot
read in a minute is a skill it cannot really approve.

The contract:

* one function, ``run(inputs, ctx)``, returning ``{"speech": str, "data": dict}``;
* the only way out is ``ctx.fetch_json`` / ``ctx.fetch_text``, and it can only
  reach the hosts named in manifest.json -- the sandbox has no network of its
  own, so there is no second way even by accident;
* no imports, no files, no environment, no clock beyond what the answer carries.

Source: https://wheretheiss.at/w/developer (public, https, no API key).
"""

URL = "https://api.wheretheiss.at/v1/satellites/25544"


def run(inputs, ctx):
    reading = ctx.fetch_json(URL)

    latitude = round(float(reading["latitude"]), 1)
    longitude = round(float(reading["longitude"]), 1)
    altitude_km = int(round(float(reading["altitude"])))
    velocity_kmh = int(round(float(reading["velocity"])))

    speech = (
        "The ISS is at {lat}, {lon}, about {alt} km up, "
        "moving {vel} km/h.".format(
            lat=latitude, lon=longitude, alt=altitude_km, vel=velocity_kmh)
    )
    return {
        "speech": speech,
        "data": {
            "latitude": latitude,
            "longitude": longitude,
            "altitude_km": altitude_km,
            "velocity_kmh": velocity_kmh,
            "source": URL,
        },
    }
