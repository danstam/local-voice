"""Microphone discovery without opening a stream or changing system defaults."""


def input_devices():
    import sounddevice as sd
    apis = sd.query_hostapis()
    return [dict(index=index, name=device["name"], hostapi=apis[device["hostapi"]]["name"],
                 label=f"{device['name']} ({apis[device['hostapi']]['name']})")
            for index, device in enumerate(sd.query_devices()) if device["max_input_channels"] > 0]


def resolve_input_device(preference):
    if preference is None:
        return None
    if isinstance(preference, dict):
        for device in input_devices():
            if all(device[key] == preference.get(key) for key in ("name", "hostapi")):
                return device["index"]
        raise RuntimeError("The selected microphone is unavailable. Choose a microphone in Settings.")
    return preference
