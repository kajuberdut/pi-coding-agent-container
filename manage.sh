#!/usr/bin/env bash
# Delegate everything to the uv-managed python package pib
exec uv run pib "$@"
