"""Voice adapter between an ESPHome voice device and Hermes Agent.

The device provides the wake word, the microphone and the speaker; speech to text and
text to speech are HTTP calls to a hosted endpoint (Mistral Voxtral by default) and the
agent turn is one `hermes -z` invocation. Nothing runs locally besides this process.
"""

__version__ = "0.1.0"
