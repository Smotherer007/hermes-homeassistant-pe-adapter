#!/bin/sh
# `answer_cmd` wrapper for hermes-pe-adapter.
#
# The adapter passes the transcript as ONE argv element, so it is never re-split and
# never reaches a shell. This wrapper puts the spoken-answer instruction in front of it
# and keeps the agent call in one place, so the config stays readable:
#
#   "answer_cmd": "hermes-voice {text}"
#
#   PE_HERMES       agent binary        (default: hermes from PATH)
#   PE_INSTRUCTION  instruction prefix  (default: two sentences, no lists)
#
# Anything the agent prints on stdout is what gets spoken; stderr is ignored by the
# adapter, so keep diagnostics out of stdout.
set -u

: "${PE_HERMES:=hermes}"
: "${PE_INSTRUCTION:=Antworte in hoechstens zwei Saetzen, ohne Aufzaehlungen:}"

exec "$PE_HERMES" -z "$PE_INSTRUCTION $1"
