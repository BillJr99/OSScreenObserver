"""Shared test fixtures."""
from __future__ import annotations

import pytest

from main import _DEFAULT_CONFIG
from observer import ScreenObserver
from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
from session import reset_session_for_tests


@pytest.fixture()
def config():
    cfg = dict(_DEFAULT_CONFIG)
    cfg["mock"] = True
    return cfg


@pytest.fixture()
def fresh_session():
    yield reset_session_for_tests()
    reset_session_for_tests()


@pytest.fixture()
def observer(config, fresh_session):
    return ScreenObserver(config)


@pytest.fixture()
def renderer(config):
    return ASCIIRenderer(config)


@pytest.fixture()
def describer(config):
    return DescriptionGenerator(config)


@pytest.fixture()
def app(config, observer, renderer, describer):
    from web_inspector import create_web_app
    return create_web_app(observer, renderer, describer, config)


@pytest.fixture()
def client(app):
    return app.test_client()
