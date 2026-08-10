"""
Tests for src/gateway/mqtt.py (Task 18): MqttPublisher, a port of
src/http_gateway.py's HTTPGateway.connect_mqtt()/forward_to_mqtt(). No
live MQTT broker is available in this environment -- paho.mqtt.client is
fully mocked; these tests verify connection parameters and
topic/payload/QoS construction, not real network behavior.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway.config import GatewayConfig  # noqa: E402
from gateway.mqtt import MqttPublisher  # noqa: E402
import gateway.mqtt as gateway_mqtt_module  # noqa: E402


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "d" * 32)
    monkeypatch.setenv("MQTT_BROKER", "test-broker")
    monkeypatch.setenv("MQTT_PORT", "1884")
    monkeypatch.setenv("MQTT_INPUT_TOPIC", "alerts/test-input")
    return GatewayConfig.from_env()


# ---------------------------------------------------------------------------
# connect_mqtt()
# ---------------------------------------------------------------------------

def test_connect_mqtt_uses_callback_api_v2_and_correct_broker(config, mocker):
    fake_client = mocker.MagicMock()
    mock_client_cls = mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    result = publisher.connect_mqtt()

    assert result is True
    _, kwargs = mock_client_cls.call_args
    assert kwargs["callback_api_version"] == gateway_mqtt_module.mqtt.CallbackAPIVersion.VERSION2
    assert kwargs["client_id"].startswith("gateway-")
    fake_client.connect.assert_called_once_with("test-broker", 1884, 60)
    fake_client.loop_start.assert_called_once_with()


def test_connect_mqtt_returns_false_and_does_not_raise_on_failure(config, mocker):
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", side_effect=Exception("boom"))

    publisher = MqttPublisher(config)

    assert publisher.connect_mqtt() is False


# ---------------------------------------------------------------------------
# is_connected()
# ---------------------------------------------------------------------------

def test_is_connected_false_before_any_connect_attempt(config):
    publisher = MqttPublisher(config)
    assert publisher.is_connected() is False


def test_is_connected_reflects_underlying_client(config, mocker):
    fake_client = mocker.MagicMock()
    fake_client.is_connected.return_value = True
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    publisher.connect_mqtt()

    assert publisher.is_connected() is True


# ---------------------------------------------------------------------------
# forward_to_mqtt(): topic/payload/QoS construction (the ported logic)
# ---------------------------------------------------------------------------

def test_forward_to_mqtt_publishes_correct_topic_payload_and_qos(config, mocker):
    fake_client = mocker.MagicMock()
    fake_client.is_connected.return_value = True
    fake_result = mocker.MagicMock(rc=gateway_mqtt_module.mqtt.MQTT_ERR_SUCCESS)
    fake_client.publish.return_value = fake_result
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    publisher.connect_mqtt()

    alert_data = {"timestamp": 1234567890, "level": "info", "category": "system",
                  "title": "t", "message": "m", "source": "s"}
    result = publisher.forward_to_mqtt(alert_data)

    assert result is True
    fake_client.publish.assert_called_once_with(
        "alerts/test-input", json.dumps(alert_data), qos=1
    )


def test_forward_to_mqtt_reconnects_when_not_currently_connected(config, mocker):
    fake_client = mocker.MagicMock()
    fake_client.is_connected.return_value = False  # never becomes True in this test
    fake_result = mocker.MagicMock(rc=gateway_mqtt_module.mqtt.MQTT_ERR_SUCCESS)
    fake_client.publish.return_value = fake_result
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    connect_spy = mocker.spy(publisher, "connect_mqtt")

    publisher.forward_to_mqtt({"foo": "bar"})

    connect_spy.assert_called_once_with()


def test_forward_to_mqtt_returns_false_when_publish_rc_is_not_success(config, mocker):
    fake_client = mocker.MagicMock()
    fake_client.is_connected.return_value = True
    fake_result = mocker.MagicMock(rc=1)  # any non-zero/non-MQTT_ERR_SUCCESS code
    fake_client.publish.return_value = fake_result
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    publisher.connect_mqtt()

    assert publisher.forward_to_mqtt({"foo": "bar"}) is False


def test_forward_to_mqtt_returns_false_and_does_not_raise_on_exception(config, mocker):
    fake_client = mocker.MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.publish.side_effect = Exception("network error")
    mocker.patch.object(gateway_mqtt_module.mqtt, "Client", return_value=fake_client)

    publisher = MqttPublisher(config)
    publisher.connect_mqtt()

    assert publisher.forward_to_mqtt({"foo": "bar"}) is False
