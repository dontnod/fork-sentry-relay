from datetime import datetime, timedelta, timezone
import json
import signal

import pytest
import requests

from .test_envelope import generate_transaction_item

TEST_CONFIG = {
    "aggregator": {"bucket_interval": 1, "initial_delay": 0, "debounce_delay": 0}
}


def _session_payload(timestamp: datetime, started: datetime):
    return {
        "sid": "8333339f-5675-4f89-a9a0-1c935255ab58",
        "did": "foobarbaz",
        "seq": 42,
        "init": True,
        "timestamp": timestamp.isoformat(),
        "started": started.isoformat(),
        "duration": 1947.49,
        "status": "exited",
        "errors": 0,
        "attrs": {"release": "sentry-test@1.0.0", "environment": "production"},
    }


def metrics_by_name(metrics_consumer, count, timeout=None):
    metrics = {}

    for _ in range(count):
        metric = metrics_consumer.get_metric(timeout)
        metrics[metric["name"]] = metric

    metrics_consumer.assert_empty()
    return metrics


def test_metrics(mini_sentry, relay):
    relay = relay(mini_sentry, options=TEST_CONFIG)

    project_id = 42
    mini_sentry.add_basic_project_config(project_id)

    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    metrics_payload = f"transactions/foo:42|c\ntransactions/bar:17|c"
    relay.send_metrics(project_id, metrics_payload, timestamp)

    envelope = mini_sentry.captured_events.get(timeout=3)
    assert len(envelope.items) == 1

    metrics_item = envelope.items[0]
    assert metrics_item.type == "metric_buckets"

    received_metrics = json.loads(metrics_item.get_bytes().decode())
    received_metrics = sorted(received_metrics, key=lambda x: x["name"])
    assert received_metrics == [
        {
            "timestamp": timestamp,
            "width": 1,
            "name": "c:transactions/bar@none",
            "value": 17.0,
            "type": "c",
        },
        {
            "timestamp": timestamp,
            "width": 1,
            "name": "c:transactions/foo@none",
            "value": 42.0,
            "type": "c",
        },
    ]


def test_metrics_backdated(mini_sentry, relay):
    relay = relay(mini_sentry, options=TEST_CONFIG)

    project_id = 42
    mini_sentry.add_basic_project_config(project_id)

    timestamp = int(datetime.now(tz=timezone.utc).timestamp()) - 24 * 60 * 60
    metrics_payload = f"transactions/foo:42|c"
    relay.send_metrics(project_id, metrics_payload, timestamp)

    envelope = mini_sentry.captured_events.get(timeout=2)
    assert len(envelope.items) == 1

    metrics_item = envelope.items[0]
    assert metrics_item.type == "metric_buckets"

    received_metrics = metrics_item.get_bytes()
    assert json.loads(received_metrics.decode()) == [
        {
            "timestamp": timestamp,
            "width": 1,
            "name": "c:transactions/foo@none",
            "value": 42.0,
            "type": "c",
        },
    ]


@pytest.mark.parametrize(
    "flush_partitions,expected_header", [(None, None), (0, "0"), (1, "0"), (128, "34")]
)
def test_metrics_partition_key(mini_sentry, relay, flush_partitions, expected_header):
    forever = 100 * 365 * 24 * 60 * 60  # *almost forever
    relay_config = {
        "processing": {
            "max_session_secs_in_past": forever,
        },
        "aggregator": {
            "bucket_interval": 1,
            "initial_delay": 0,
            "debounce_delay": 0,
            "flush_partitions": flush_partitions,
            "max_secs_in_past": forever,
            "max_secs_in_future": forever,
        },
    }
    relay = relay(mini_sentry, options=relay_config)

    project_id = 42
    mini_sentry.add_basic_project_config(
        project_id,
        dsn_public_key={  # Need explicit DSN to get a consistent partition key
            "publicKey": "31a5a894b4524f74a9a8d0e27e21ba91",
            "isEnabled": True,
            "numericId": 42,
        },
    )

    timestamp = 999994711
    metrics_payload = f"transactions/foo:42|c"
    relay.send_metrics(project_id, metrics_payload, timestamp)

    mini_sentry.captured_events.get(timeout=3)

    headers, _ = mini_sentry.request_log[-1]
    if expected_header is None:
        assert "X-Sentry-Relay-Shard" not in headers
    else:
        assert headers["X-Sentry-Relay-Shard"] == expected_header, headers


def test_metrics_with_processing(mini_sentry, relay_with_processing, metrics_consumer):
    relay = relay_with_processing(options=TEST_CONFIG)
    metrics_consumer = metrics_consumer()

    project_id = 42
    mini_sentry.add_full_project_config(project_id)

    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    metrics_payload = f"transactions/foo:42|c\ntransactions/bar@second:17|c"
    relay.send_metrics(project_id, metrics_payload, timestamp)

    metrics = metrics_by_name(metrics_consumer, 2)

    assert metrics["c:transactions/foo@none"] == {
        "org_id": 1,
        "project_id": project_id,
        "name": "c:transactions/foo@none",
        "value": 42.0,
        "type": "c",
        "timestamp": timestamp,
    }

    assert metrics["c:transactions/bar@second"] == {
        "org_id": 1,
        "project_id": project_id,
        "name": "c:transactions/bar@second",
        "value": 17.0,
        "type": "c",
        "timestamp": timestamp,
    }


def test_metrics_with_sharded_kafka(
    get_topic_name,
    mini_sentry,
    relay_with_processing,
    metrics_consumer,
    processing_config,
):
    options = processing_config(None)
    default_config = options["processing"]["kafka_config"]
    config = {
        "processing": {
            "secondary_kafka_configs": {"foo": default_config, "baz": default_config},
            "topics": {
                "metrics": {
                    "shards": 3,
                    "mapping": {
                        0: {
                            "name": get_topic_name("metrics-1"),
                            "config": "foo",
                        },
                        2: {
                            "name": get_topic_name("metrics-2"),
                            "config": "baz",
                        },
                    },
                }
            },
        }
    }
    relay = relay_with_processing(options={**TEST_CONFIG, **config})

    project_id = 42
    project_config = mini_sentry.add_full_project_config(project_id)
    project_config["organizationId"] = 5
    m1 = metrics_consumer(topic="metrics-1")
    m2 = metrics_consumer(topic="metrics-2")

    metrics_payload = f"transactions/foo:42|c\ntransactions/bar@second:17|c"

    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    relay.send_metrics(project_id, metrics_payload, timestamp)

    # There must be no messages in the metrics-1, since the shard was not picked up
    m1.assert_empty()

    metrics2 = metrics_by_name(m2, 2)
    assert metrics2["c:transactions/foo@none"] == {
        "org_id": 5,
        "project_id": project_id,
        "name": "c:transactions/foo@none",
        "value": 42.0,
        "type": "c",
        "timestamp": timestamp,
    }

    assert metrics2["c:transactions/bar@second"] == {
        "org_id": 5,
        "project_id": project_id,
        "name": "c:transactions/bar@second",
        "value": 17.0,
        "type": "c",
        "timestamp": timestamp,
    }


def test_metrics_full(mini_sentry, relay, relay_with_processing, metrics_consumer):
    metrics_consumer = metrics_consumer()

    upstream_config = {
        "aggregator": {
            "bucket_interval": 1,
            "initial_delay": 2,  # Give upstream some time to process downstream entries:
            "debounce_delay": 0,
        }
    }
    upstream = relay_with_processing(options=upstream_config)

    downstream = relay(upstream, options=TEST_CONFIG)

    # Create project config
    project_id = 42
    mini_sentry.add_full_project_config(project_id)

    # Send two events to downstream and one to upstream
    timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    downstream.send_metrics(project_id, f"transactions/foo:7|c", timestamp)
    downstream.send_metrics(project_id, f"transactions/foo:5|c", timestamp)

    upstream.send_metrics(project_id, f"transactions/foo:3|c", timestamp)

    metric = metrics_consumer.get_metric(timeout=6)
    metric.pop("timestamp")
    assert metric == {
        "org_id": 1,
        "project_id": project_id,
        "name": "c:transactions/foo@none",
        "value": 15.0,
        "type": "c",
    }

    metrics_consumer.assert_empty()


@pytest.mark.parametrize(
    "extract_metrics", [True, False], ids=["extract", "don't extract"]
)
@pytest.mark.parametrize(
    "metrics_extracted", [True, False], ids=["extracted", "not extracted"]
)
def test_session_metrics_non_processing(
    mini_sentry, relay, extract_metrics, metrics_extracted
):
    """
    Tests metrics extraction in  a non processing relay

    If and only if the metrics-extraction feature is enabled and the metrics from the session were not already
    extracted the relay should extract the metrics from the session and mark the session item as "metrics extracted"
    """

    relay = relay(mini_sentry, options=TEST_CONFIG)

    if extract_metrics:
        # enable metrics extraction for the project
        extra_config = {"config": {"sessionMetrics": {"version": 1}}}
    else:
        extra_config = {}

    project_id = 42
    mini_sentry.add_basic_project_config(project_id, extra=extra_config)

    timestamp = datetime.now(tz=timezone.utc)
    started = timestamp - timedelta(hours=1)
    session_payload = _session_payload(timestamp=timestamp, started=started)

    relay.send_session(
        project_id,
        session_payload,
        item_headers={"metrics_extracted": metrics_extracted},
    )

    # Get session envelope
    first_envelope = mini_sentry.captured_events.get(timeout=2)

    try:
        second_envelope = mini_sentry.captured_events.get(timeout=2)
    except Exception:
        second_envelope = None

    assert first_envelope is not None
    assert len(first_envelope.items) == 1
    first_item = first_envelope.items[0]

    if extract_metrics and not metrics_extracted:
        # here we have not yet extracted metrics and metric extraction is enabled
        # we expect to have two messages a session message and a metrics message
        assert second_envelope is not None
        assert len(second_envelope.items) == 1

        second_item = second_envelope.items[0]

        if first_item.type == "session":
            session_item = first_item
            metrics_item = second_item
        else:
            session_item = second_item
            metrics_item = first_item

        # check the metrics item
        assert metrics_item.type == "metric_buckets"

        session_metrics = json.loads(metrics_item.get_bytes().decode())
        session_metrics = sorted(session_metrics, key=lambda x: x["name"])

        ts = int(started.timestamp())
        assert session_metrics == [
            {
                "name": "c:sessions/session@none",
                "tags": {
                    "sdk": "raven-node/2.6.3",
                    "environment": "production",
                    "release": "sentry-test@1.0.0",
                    "session.status": "init",
                },
                "timestamp": ts,
                "width": 1,
                "type": "c",
                "value": 1.0,
            },
            {
                "name": "s:sessions/user@none",
                "tags": {
                    "sdk": "raven-node/2.6.3",
                    "environment": "production",
                    "release": "sentry-test@1.0.0",
                },
                "timestamp": ts,
                "width": 1,
                "type": "s",
                "value": [1617781333],
            },
        ]
    else:
        # either the metrics are already extracted or we have metric extraction disabled
        # only the session message should be present
        assert second_envelope is None
        session_item = first_item

    assert session_item is not None
    assert session_item.type == "session"

    # we have marked the item as "metrics extracted" properly
    # already extracted metrics should keep the flag, newly extracted metrics should set the flag
    assert (
        session_item.headers.get("metrics_extracted", False) is extract_metrics
        or metrics_extracted
    )


def test_session_metrics_extracted_only_once(
    mini_sentry, relay, relay_with_processing, metrics_consumer
):
    """
    Tests that a chain of multiple relays only extracts metrics once

    Create a chain with 3 relays (all with metric extraction), and check that only the first
    relay does the extraction and the following relays just pass the metrics through
    """

    relay_chain = relay(
        relay(relay_with_processing(options=TEST_CONFIG), options=TEST_CONFIG),
        options=TEST_CONFIG,
    )

    # enable metrics extraction for the project
    extra_config = {"config": {"sessionMetrics": {"version": 1}}}

    project_id = 42
    mini_sentry.add_full_project_config(project_id, extra=extra_config)

    metrics_consumer = metrics_consumer()

    timestamp = datetime.now(tz=timezone.utc)
    started = timestamp - timedelta(hours=1)
    session_payload = _session_payload(timestamp=timestamp, started=started)

    relay_chain.send_session(project_id, session_payload)

    metrics = metrics_by_name(metrics_consumer, 2, timeout=6)

    # if it is not 1 it means the session was extracted multiple times
    assert metrics["c:sessions/session@none"]["value"] == 1.0


@pytest.mark.parametrize(
    "metrics_extracted", [True, False], ids=["extracted", "not extracted"]
)
def test_session_metrics_processing(
    mini_sentry, relay_with_processing, metrics_consumer, metrics_extracted
):
    """
    Tests that a processing relay with metrics-extraction enabled creates metrics
    from sessions if the metrics were not already extracted before.
    """
    relay = relay_with_processing(options=TEST_CONFIG)
    project_id = 42

    # enable metrics extraction for the project
    extra_config = {"config": {"sessionMetrics": {"version": 1}}}

    mini_sentry.add_full_project_config(project_id, extra=extra_config)

    metrics_consumer = metrics_consumer()

    timestamp = datetime.now(tz=timezone.utc)
    started = timestamp - timedelta(hours=1)
    session_payload = _session_payload(timestamp=timestamp, started=started)

    relay.send_session(
        project_id,
        session_payload,
        item_headers={"metrics_extracted": metrics_extracted},
    )

    if metrics_extracted:
        metrics_consumer.assert_empty(timeout=2)
        return

    metrics = metrics_by_name(metrics_consumer, 2)

    expected_timestamp = int(started.timestamp())
    assert metrics["c:sessions/session@none"] == {
        "org_id": 1,
        "project_id": 42,
        "timestamp": expected_timestamp,
        "name": "c:sessions/session@none",
        "type": "c",
        "value": 1.0,
        "tags": {
            "sdk": "raven-node/2.6.3",
            "environment": "production",
            "release": "sentry-test@1.0.0",
            "session.status": "init",
        },
    }

    assert metrics["s:sessions/user@none"] == {
        "org_id": 1,
        "project_id": 42,
        "timestamp": expected_timestamp,
        "name": "s:sessions/user@none",
        "type": "s",
        "value": [1617781333],
        "tags": {
            "sdk": "raven-node/2.6.3",
            "environment": "production",
            "release": "sentry-test@1.0.0",
        },
    }


@pytest.mark.parametrize(
    "extract_metrics,discard_data,with_external_relay",
    [
        (True, "transaction", True),
        (True, "transaction", False),
        (True, "trace", False),
        (True, False, True),
        (True, False, False),
        (False, "transaction", False),
        (False, False, False),
        ("corrupted", "transaction", False),
    ],
    ids=[
        "extract from transaction-sampled, external relay",
        "extract from transaction-sampled",
        "extract from trace-sampled",
        "extract from unsampled, external relay",
        "extract from unsampled",
        "don't extract from transaction-sampled",
        "don't extract from unsampled",
        "corrupted config",
    ],
)
def test_transaction_metrics(
    mini_sentry,
    relay,
    relay_with_processing,
    metrics_consumer,
    extract_metrics,
    discard_data,
    transactions_consumer,
    with_external_relay,
):
    metrics_consumer = metrics_consumer()
    transactions_consumer = transactions_consumer()

    if with_external_relay:
        relay = relay(relay_with_processing(options=TEST_CONFIG), options=TEST_CONFIG)
    else:
        relay = relay_with_processing(options=TEST_CONFIG)

    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    timestamp = datetime.now(tz=timezone.utc)

    if extract_metrics:
        config["sessionMetrics"] = {"version": 1}
    config["breakdownsV2"] = {
        "span_ops": {"type": "spanOperations", "matches": ["react.mount"]}
    }

    if discard_data:
        # Make sure Relay drops the transaction
        config.setdefault("dynamicSampling", {}).setdefault("rules", []).append(
            {
                "sampleRate": 0,
                "type": discard_data,
                "condition": {"op": "and", "inner": []},
                "id": 1,
            }
        )

    if extract_metrics == "corrupted":
        config["transactionMetrics"] = 42

    elif extract_metrics:
        config["transactionMetrics"] = {
            "version": 1,
        }

    transaction = generate_transaction_item()
    transaction["timestamp"] = timestamp.isoformat()
    transaction["measurements"] = {
        "foo": {"value": 1.2},
        "bar": {"value": 1.3},
    }

    relay.send_transaction(42, transaction)

    # Send another transaction:
    transaction["measurements"] = {
        "foo": {"value": 2.2},
    }
    relay.send_transaction(42, transaction)

    if discard_data:
        transactions_consumer.assert_empty()
    else:
        event, _ = transactions_consumer.get_event()
        if with_external_relay:
            # there is some rounding error while serializing/deserializing
            # timestamps... haven't investigated too closely
            span_time = 9.910107
        else:
            span_time = 9.910106

        assert event["breakdowns"] == {
            "span_ops": {
                "ops.react.mount": {"value": span_time, "unit": "millisecond"},
                "total.time": {"value": span_time, "unit": "millisecond"},
            }
        }

    if not extract_metrics or extract_metrics == "corrupted":
        message = metrics_consumer.poll(timeout=None)
        assert message is None, message.value()

        return

    metrics = metrics_by_name(metrics_consumer, 4)

    common = {
        "timestamp": int(timestamp.timestamp()),
        "org_id": 1,
        "project_id": 42,
        "tags": {
            "transaction": "/organizations/:orgId/performance/:eventSlug/",
            "platform": "other",
            "transaction.status": "unknown",
        },
    }

    assert metrics["d:transactions/measurements.foo@none"] == {
        **common,
        "name": "d:transactions/measurements.foo@none",
        "type": "d",
        "value": [1.2, 2.2],
    }

    assert metrics["d:transactions/measurements.bar@none"] == {
        **common,
        "name": "d:transactions/measurements.bar@none",
        "type": "d",
        "value": [1.3],
    }

    assert metrics[
        "d:transactions/breakdowns.span_ops.ops.react.mount@millisecond"
    ] == {
        **common,
        "name": "d:transactions/breakdowns.span_ops.ops.react.mount@millisecond",
        "type": "d",
        "value": [9.910106, 9.910106],
    }


@pytest.mark.parametrize(
    "send_extracted_header,expect_extracted_header,expect_metrics_extraction",
    [(False, True, True), (True, True, False)],
    ids=["must extract metrics", "mustn't extract metrics"],
)
def test_transaction_metrics_extraction_external_relays(
    mini_sentry,
    relay,
    send_extracted_header,
    expect_extracted_header,
    expect_metrics_extraction,
):
    if send_extracted_header:
        item_headers = {"metrics_extracted": True}
    else:
        item_headers = None

    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    config["transactionMetrics"] = {
        "version": 1,
    }

    tx = generate_transaction_item()
    # Default timestamp is so old that relay drops metrics, setting a more recent one avoids the drop.
    timestamp = datetime.now(tz=timezone.utc)
    tx["timestamp"] = timestamp.isoformat()

    external = relay(mini_sentry, options=TEST_CONFIG)
    external.send_transaction(project_id, tx, item_headers)

    envelope = mini_sentry.captured_events.get(timeout=3)
    assert len(envelope.items) == 1
    tx_item = envelope.items[0]

    if expect_extracted_header:
        assert tx_item.headers["metrics_extracted"] == True
    else:
        assert "metrics_extracted" not in tx_item.headers

    tx_item_body = json.loads(tx_item.get_bytes().decode())
    assert (
        tx_item_body["transaction"] == "/organizations/:orgId/performance/:eventSlug/"
    )

    if expect_metrics_extraction:
        metrics_envelope = mini_sentry.captured_events.get(timeout=3)
        assert len(metrics_envelope.items) == 1
        m_item_body = json.loads(metrics_envelope.items[0].get_bytes().decode())
        assert len(m_item_body) == 1
        assert m_item_body[0]["name"] == "d:transactions/duration@millisecond"
        assert (
            m_item_body[0]["tags"]["transaction"]
            == "/organizations/:orgId/performance/:eventSlug/"
        )

    assert mini_sentry.captured_events.empty()


@pytest.mark.parametrize(
    "send_extracted_header,expect_metrics_extraction",
    [(False, True), (True, False)],
    ids=["must extract metrics", "mustn't extract metrics"],
)
def test_transaction_metrics_extraction_processing_relays(
    transactions_consumer,
    metrics_consumer,
    mini_sentry,
    relay_with_processing,
    send_extracted_header,
    expect_metrics_extraction,
):
    if send_extracted_header:
        item_headers = {"metrics_extracted": True}
    else:
        item_headers = None

    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    config["transactionMetrics"] = {
        "version": 1,
    }

    tx = generate_transaction_item()
    # Default timestamp is so old that relay drops metrics, setting a more recent one avoids the drop.
    timestamp = datetime.now(tz=timezone.utc)
    tx["timestamp"] = timestamp.isoformat()

    metrics_consumer = metrics_consumer()
    tx_consumer = transactions_consumer()
    processing = relay_with_processing(options=TEST_CONFIG)
    processing.send_transaction(project_id, tx, item_headers)

    tx, _ = tx_consumer.get_event()
    assert tx["transaction"] == "/organizations/:orgId/performance/:eventSlug/"
    tx_consumer.assert_empty()

    if expect_metrics_extraction:
        metric = metrics_consumer.get_metric(timeout=3)
        assert metric["name"] == "d:transactions/duration@millisecond"
        assert (
            metric["tags"]["transaction"]
            == "/organizations/:orgId/performance/:eventSlug/"
        )

    metrics_consumer.assert_empty()


@pytest.mark.parametrize(
    "unsupported_version",
    [0, 1234567890],
    ids=["version is too small", "version is too big"],
)
def test_transaction_metrics_not_extracted_on_unsupported_version(
    metrics_consumer,
    transactions_consumer,
    mini_sentry,
    relay_with_processing,
    unsupported_version,
):
    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    config["transactionMetrics"] = {
        "version": unsupported_version,
    }

    tx = generate_transaction_item()
    # Default timestamp is so old that relay drops metrics, setting a more recent one avoids the drop.
    timestamp = datetime.now(tz=timezone.utc)
    tx["timestamp"] = timestamp.isoformat()

    metrics_consumer = metrics_consumer()
    tx_consumer = transactions_consumer()

    relay = relay_with_processing(options=TEST_CONFIG)
    relay.send_transaction(project_id, tx)

    tx, _ = tx_consumer.get_event()
    assert tx["transaction"] == "/organizations/:orgId/performance/:eventSlug/"
    tx_consumer.assert_empty()

    metrics_consumer.assert_empty()


def test_no_transaction_metrics_when_filtered(mini_sentry, relay):
    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    config["transactionMetrics"] = {
        "version": 1,
    }
    config["filterSettings"]["releases"] = {"releases": ["foo@1.2.4"]}

    tx = generate_transaction_item()
    tx["release"] = "foo@1.2.4"
    # Default timestamp is so old that relay drops metrics, setting a more recent one avoids the drop.
    timestamp = datetime.now(tz=timezone.utc)
    tx["timestamp"] = timestamp.isoformat()

    relay = relay(mini_sentry, options=TEST_CONFIG)
    relay.send_transaction(project_id, tx)

    # The only envelope received should be outcomes:
    envelope = mini_sentry.captured_events.get(timeout=3)
    assert {item.type for item in envelope.items} == {"client_report"}

    assert mini_sentry.captured_events.qsize() == 0


def test_graceful_shutdown(mini_sentry, relay):
    relay = relay(
        mini_sentry,
        options={
            "limits": {"shutdown_timeout": 2},
            "aggregator": {
                "bucket_interval": 1,
                "initial_delay": 10,
                "debounce_delay": 0,
            },
        },
    )

    project_id = 42
    mini_sentry.add_basic_project_config(project_id)

    timestamp = int(datetime.now(tz=timezone.utc).timestamp())

    # Backdated metric will be flushed immediately due to debounce delay
    past_timestamp = timestamp - 1000
    metrics_payload = f"transactions/foo:42|c"
    relay.send_metrics(project_id, metrics_payload, past_timestamp)

    # Future timestamp will not be flushed regularly, only through force flush
    metrics_payload = f"transactions/bar:17|c"
    future_timestamp = timestamp + 30
    relay.send_metrics(project_id, metrics_payload, future_timestamp)
    relay.shutdown(sig=signal.SIGTERM)

    # Try to send another metric (will be rejected)
    metrics_payload = f"transactions/zap:666|c"
    with pytest.raises(requests.ConnectionError):
        relay.send_metrics(project_id, metrics_payload, timestamp)

    envelope = mini_sentry.captured_events.get(timeout=5)
    assert len(envelope.items) == 1
    metrics_item = envelope.items[0]
    assert metrics_item.type == "metric_buckets"
    received_metrics = json.loads(metrics_item.get_bytes().decode())
    received_metrics = sorted(received_metrics, key=lambda x: x["name"])
    assert received_metrics == [
        {
            "timestamp": future_timestamp,
            "width": 1,
            "name": "c:transactions/bar@none",
            "value": 17.0,
            "type": "c",
        },
        {
            "timestamp": past_timestamp,
            "width": 1,
            "name": "c:transactions/foo@none",
            "value": 42.0,
            "type": "c",
        },
    ]


def test_limit_custom_measurements(
    mini_sentry, relay, relay_with_processing, metrics_consumer, transactions_consumer
):
    """Custom measurement config is propagated to outer relay"""
    metrics_consumer = metrics_consumer()
    transactions_consumer = transactions_consumer()

    relay = relay(relay_with_processing(options=TEST_CONFIG), options=TEST_CONFIG)

    project_id = 42
    mini_sentry.add_full_project_config(project_id)
    config = mini_sentry.project_configs[project_id]["config"]
    timestamp = datetime.now(tz=timezone.utc)

    config["measurements"] = {
        "builtinMeasurements": [{"name": "foo", "unit": "none"}],
        "maxCustomMeasurements": 1,
    }
    config["transactionMetrics"] = {
        "version": 1,
    }

    transaction = generate_transaction_item()
    transaction["timestamp"] = timestamp.isoformat()
    transaction["measurements"] = {
        "foo": {"value": 1.2},
        "baz": {
            "value": 1.3
        },  # baz comes before bar, but custom measurements are picked in alphabetical order
        "bar": {"value": 1.4},
    }

    relay.send_transaction(42, transaction)

    event, _ = transactions_consumer.get_event()
    assert len(event["measurements"]) == 2

    # Expect exactly 3 metrics (transaction.duration, 1 builtin, 1 custom)
    metrics = metrics_by_name(metrics_consumer, 3)

    assert metrics.keys() == {
        "d:transactions/duration@millisecond",
        "d:transactions/measurements.foo@none",
        "d:transactions/measurements.bar@none",
    }
