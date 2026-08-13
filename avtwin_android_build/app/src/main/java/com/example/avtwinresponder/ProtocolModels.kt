package com.example.avtwinresponder

import java.security.MessageDigest
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

object Sha256 {
    fun hex(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(Locale.US, it.toInt() and 0xff) }

    fun pcm16Hex(samples: ShortArray): String {
        val bytes = ByteArray(samples.size * 2)
        var o = 0
        for (s in samples) {
            val v = s.toInt()
            bytes[o++] = (v and 0xff).toByte()
            bytes[o++] = ((v ushr 8) and 0xff).toByte()
        }
        return hex(bytes)
    }
}

object JsonWire {
    fun escape(s: String): String = s
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")

    fun obj(vararg fields: Pair<String, Any?>): String = buildString {
        append('{')
        fields.forEachIndexed { index, (key, value) ->
            if (index > 0) append(',')
            append('"').append(escape(key)).append("\":")
            append(valueToJson(value))
        }
        append('}')
    }

    private fun valueToJson(v: Any?): String = when (v) {
        null -> "null"
        is String -> "\"${escape(v)}\""
        is Boolean -> if (v) "true" else "false"
        is Number -> v.toString()
        else -> "\"${escape(v.toString())}\""
    }

    fun stringField(json: String, key: String): String? {
        val r = Regex("\\\"${Regex.escape(key)}\\\"\\s*:\\s*\\\"((?:\\\\.|[^\\\"])*)\\\"")
        val raw = r.find(json)?.groupValues?.getOrNull(1) ?: return null
        return raw.replace("\\\"", "\"").replace("\\\\", "\\")
    }

    fun longField(json: String, key: String): Long? {
        val r = Regex("\\\"${Regex.escape(key)}\\\"\\s*:\\s*(-?[0-9]+)")
        return r.find(json)?.groupValues?.getOrNull(1)?.toLongOrNull()
    }

    fun booleanField(json: String, key: String): Boolean? {
        val r = Regex("\\\"${Regex.escape(key)}\\\"\\s*:\\s*(true|false)", RegexOption.IGNORE_CASE)
        return when (r.find(json)?.groupValues?.getOrNull(1)?.lowercase(Locale.US)) {
            "true" -> true
            "false" -> false
            else -> null
        }
    }
}

/**
 * Process-wide gate for the Android responder's formal experiment mode.
 *
 * It is deliberately NOT a timing source. It only decides whether the detector is allowed
 * to associate the next acoustic C1 with a Linux measurement. Every accepted ARM advances
 * a generation so StreamingC1Detector can discard samples buffered before that ARM.
 */
object StrictArmGate {
    private val generationCounter = AtomicLong(0L)
    @Volatile private var armed = false

    fun reset() {
        armed = false
        generationCounter.incrementAndGet()
    }

    fun arm() {
        armed = true
        generationCounter.incrementAndGet()
    }

    fun consume() {
        armed = false
    }

    fun isArmed(): Boolean = armed
    fun generation(): Long = generationCounter.get()
}

data class ArmCommand(
    val protocolVersion: Int,
    val sessionId: String,
    val measurementId: Long,
    val armEventId: String
) {
    companion object {
        fun parse(json: String): ArmCommand? {
            if (JsonWire.stringField(json, "type") != "arm") return null
            val version = JsonWire.longField(json, "protocol_version")?.toInt() ?: return null
            val session = JsonWire.stringField(json, "session_id")?.takeIf { it.isNotBlank() } ?: return null
            val measurement = JsonWire.longField(json, "measurement_id") ?: return null
            val eventId = JsonWire.stringField(json, "arm_event_id")?.takeIf { it.isNotBlank() } ?: return null
            return ArmCommand(version, session, measurement, eventId)
        }
    }
}

data class PairingClaim(
    val sessionId: String,
    val measurementId: Long,
    val pairingMode: String,
    val armReceivedDiagnosticMs: Long?
)

/**
 * Strict protocol semantics:
 *   one accepted ARM -> at most one claim -> at most one acoustic response.
 *
 * There is intentionally no chronological_unarmed production fallback in v0.9.0. If an
 * internal race ever attempts to claim a C1 without a fresh ARM, fail closed instead of
 * authorizing C2. elapsedRealtime values below are protocol freshness diagnostics only and
 * are never used as t2/t3 acoustic timing.
 */
class ArmPairingManager(
    currentSessionId: String? = null,
    private val maxArmAgeMs: Long = 10_000L
) {
    data class AcceptResult(val accepted: Boolean, val reason: String)

    private var pending: ArmCommand? = null
    private var boundSessionId: String? = currentSessionId
    private var pendingReceivedMs: Long = 0L
    private var lastClaimedMeasurementId: Long = Long.MIN_VALUE
    private val acceptedArmEvents = LinkedHashMap<String, Long>()

    init {
        StrictArmGate.reset()
    }

    @Synchronized
    fun accept(command: ArmCommand, nowDiagnosticMs: Long): AcceptResult {
        if (command.protocolVersion != 1) return AcceptResult(false, "unsupported_protocol_version")
        if (command.sessionId.isBlank()) return AcceptResult(false, "invalid_session_id")
        if (command.measurementId <= 0L) return AcceptResult(false, "invalid_measurement_id")
        if (command.armEventId.isBlank()) return AcceptResult(false, "invalid_arm_event_id")
        val bound = boundSessionId
        if (bound != null && command.sessionId != bound) return AcceptResult(false, "session_id_mismatch")

        // Remember accepted event IDs beyond claimNext(). An arm_ack can be lost and the
        // matching retransmission can arrive after C1 has already consumed the pending ARM.
        // Re-ACKing it must never advance StrictArmGate or authorize a second C2.
        val acceptedMeasurement = acceptedArmEvents[command.armEventId]
        if (acceptedMeasurement != null) {
            return if (acceptedMeasurement == command.measurementId) {
                AcceptResult(true, "duplicate_arm_reack")
            } else {
                AcceptResult(false, "arm_event_id_reused")
            }
        }
        if (command.measurementId <= lastClaimedMeasurementId) {
            return AcceptResult(
                false,
                if (command.measurementId == lastClaimedMeasurementId) "duplicate_measurement_new_event" else "old_measurement_id"
            )
        }
        val p = pending
        if (p != null && command.measurementId <= p.measurementId) {
            return AcceptResult(false, if (command.measurementId == p.measurementId) "duplicate_measurement_new_event" else "older_than_pending_arm")
        }
        if (boundSessionId == null) boundSessionId = command.sessionId
        pending = command
        pendingReceivedMs = nowDiagnosticMs
        acceptedArmEvents[command.armEventId] = command.measurementId
        // A bounded history is enough for delayed UDP duplicates without leaking memory in a
        // long continuous session. Measurement monotonicity still rejects older evicted events.
        if (acceptedArmEvents.size > 256) {
            val oldest = acceptedArmEvents.entries.iterator()
            if (oldest.hasNext()) {
                oldest.next()
                oldest.remove()
            }
        }
        StrictArmGate.arm()
        return AcceptResult(true, if (p == null) "accepted_strict" else "accepted_superseding_pending_strict")
    }

    @Synchronized
    fun claimNext(nowDiagnosticMs: Long): PairingClaim {
        val p = pending
        pending = null
        if (p != null) {
            val age = nowDiagnosticMs - pendingReceivedMs
            if (age in 0..maxArmAgeMs) {
                lastClaimedMeasurementId = maxOf(lastClaimedMeasurementId, p.measurementId)
                StrictArmGate.consume()
                return PairingClaim(p.sessionId, p.measurementId, "strict_armed", pendingReceivedMs)
            }
        }
        StrictArmGate.consume()
        throw IllegalStateException("STRICT ARM required or ARM expired; refusing acoustic response")
    }

    @Synchronized
    fun clearPending() {
        pending = null
        StrictArmGate.consume()
    }

    @Synchronized
    fun pendingMeasurementId(): Long? = pending?.measurementId
    @Synchronized
    fun pairedSessionId(): String? = boundSessionId
}

data class ReplyEventId(val value: String = UUID.randomUUID().toString())
