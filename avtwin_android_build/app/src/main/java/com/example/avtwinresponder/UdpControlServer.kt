package com.example.avtwinresponder

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketTimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class UdpControlServer(
    private val port: Int,
    private val onArm: (ArmCommand, String) -> ArmPairingManager.AcceptResult,
    private val onArmAckSent: (ArmCommand, ArmPairingManager.AcceptResult, String) -> Unit,
    private val onReplyAck: (String, Long, String, Boolean, String) -> Unit,
    private val onUdpTest: (String, String) -> Unit,
    private val onMalformed: (String, String) -> Unit,
    private val onError: (String) -> Unit
) {
    private val running = AtomicBoolean(false)
    private var socket: DatagramSocket? = null
    private var worker: Thread? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        worker = thread(name = "AVTwin-UDP-Control", isDaemon = true) {
            try {
                DatagramSocket(port).use { s ->
                    socket = s
                    s.soTimeout = 500
                    val buf = ByteArray(8192)
                    while (running.get()) {
                        try {
                            val packet = DatagramPacket(buf, buf.size)
                            s.receive(packet)
                            val raw = String(packet.data, packet.offset, packet.length, Charsets.UTF_8)
                            val sourceHost = packet.address.hostAddress ?: ""
                            val source = "$sourceHost:${packet.port}"

                            // Strict formal-experiment mode: only the configured Linux host may ARM
                            // the responder. This policy is orchestration only; it is never used as
                            // an acoustic timing source.
                            if (!StrictArmNetworkPolicy.sourceAllowed(sourceHost)) {
                                onMalformed(raw, "$source [STRICT_ARM_SOURCE_REJECTED expected=${StrictArmNetworkPolicy.expectedHost()}]")
                                continue
                            }

                            if (
                                JsonWire.stringField(raw, "protocol") == "AVTWIN_UDP_TEST_V1" &&
                                JsonWire.stringField(raw, "type") == "udp_test_ping"
                            ) {
                                val nonce = JsonWire.stringField(raw, "nonce")
                                if (nonce.isNullOrBlank()) {
                                    onMalformed(raw, "$source [UDP_TEST_MISSING_NONCE]")
                                } else {
                                    val reply = JsonWire.obj(
                                        "protocol" to "AVTWIN_UDP_TEST_V1",
                                        "type" to "udp_test_reply",
                                        "nonce" to nonce,
                                        "receiver" to "android"
                                    ).toByteArray(Charsets.UTF_8)
                                    s.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                                    onUdpTest(source, nonce)
                                }
                                continue
                            }

                            if (JsonWire.stringField(raw, "type") == "reply_ack") {
                                val sessionId = JsonWire.stringField(raw, "session_id")
                                val measurementId = JsonWire.longField(raw, "measurement_id")
                                val eventId = JsonWire.stringField(raw, "android_event_id")
                                val accepted = JsonWire.booleanField(raw, "accepted")
                                if (sessionId == null || measurementId == null || eventId == null || accepted == null) {
                                    onMalformed(raw, "$source [REPLY_ACK_INVALID]")
                                } else {
                                    onReplyAck(sessionId, measurementId, eventId, accepted, source)
                                }
                                continue
                            }

                            val arm = ArmCommand.parse(raw)
                            if (arm != null) {
                                val result = onArm(arm, source)
                                val ack = JsonWire.obj(
                                    "type" to "arm_ack",
                                    "protocol_version" to 1,
                                    "session_id" to arm.sessionId,
                                    "measurement_id" to arm.measurementId,
                                    "arm_event_id" to arm.armEventId,
                                    "accepted" to result.accepted,
                                    "reason" to result.reason,
                                    "receiver" to "android"
                                ).toByteArray(Charsets.UTF_8)
                                s.send(DatagramPacket(ack, ack.size, packet.address, packet.port))
                                onArmAckSent(arm, result, source)
                            } else onMalformed(raw, source)
                        } catch (_: SocketTimeoutException) {
                            // periodic wakeup for safe stop
                        }
                    }
                }
            } catch (t: Throwable) {
                if (running.get()) onError("UDP control listener error: ${t.javaClass.simpleName}: ${t.message}")
            } finally {
                socket = null
                running.set(false)
            }
        }
    }

    fun stop() {
        running.set(false)
        try { socket?.close() } catch (_: Throwable) {}
        try { worker?.join(700) } catch (_: Throwable) {}
        worker = null
    }
}
