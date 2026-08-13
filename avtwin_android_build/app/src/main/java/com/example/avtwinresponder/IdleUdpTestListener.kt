package com.example.avtwinresponder

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/** Replies to Linux UDP diagnostics while no acoustic session owns controlPort. */
class IdleUdpTestListener(
    private val port: Int,
    private val expectedLinuxHost: String,
    private val onStatus: (String) -> Unit
) {
    private val running = AtomicBoolean(false)
    private var socket: DatagramSocket? = null
    private var worker: Thread? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        worker = thread(name = "AVTwin-Idle-UDP-Test", isDaemon = true) {
            try {
                DatagramSocket(port).use { channel ->
                    socket = channel
                    channel.soTimeout = 500
                    onStatus("UDP 测试监听已就绪：0.0.0.0:$port")
                    val buffer = ByteArray(8192)
                    while (running.get()) {
                        try {
                            val packet = DatagramPacket(buffer, buffer.size)
                            channel.receive(packet)
                            val sourceHost = packet.address.hostAddress ?: ""
                            val raw = String(packet.data, packet.offset, packet.length, Charsets.UTF_8)
                            if (
                                JsonWire.stringField(raw, "protocol") != "AVTWIN_UDP_TEST_V1" ||
                                JsonWire.stringField(raw, "type") != "udp_test_ping"
                            ) continue
                            val expected = expectedLinuxHost.trim()
                            if (expected.isBlank() || sourceHost != expected) {
                                onStatus("UDP 测试已拒绝：来源 $sourceHost 与 Linux IP $expected 不一致")
                                continue
                            }
                            val nonce = JsonWire.stringField(raw, "nonce")
                            if (nonce.isNullOrBlank()) continue
                            val reply = JsonWire.obj(
                                "protocol" to "AVTWIN_UDP_TEST_V1",
                                "type" to "udp_test_reply",
                                "nonce" to nonce,
                                "receiver" to "android"
                            ).toByteArray(Charsets.UTF_8)
                            channel.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                            onStatus("UDP 测试已回复 Linux $sourceHost:${packet.port} nonce=$nonce")
                        } catch (_: SocketTimeoutException) {
                            // Wake periodically so stop() stays responsive.
                        }
                    }
                }
            } catch (error: SocketException) {
                if (running.get()) onStatus("UDP 测试监听失败：${error.message}")
            } catch (error: Throwable) {
                if (running.get()) onStatus("UDP 测试监听错误：${error.javaClass.simpleName}: ${error.message}")
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
