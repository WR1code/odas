package com.example.avtwinresponder

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/** Immutable manual pose captured at the instant a valid C1 is accepted. */
data class ManualPose(
    val xMeters: Double,
    val yMeters: Double,
    val zMeters: Double,
    val yawDegrees: Double,
    val pitchDegrees: Double,
    val rollDegrees: Double,
    val revision: Long,
    val updatedAtDiagnosticMs: Long
) {
    data class Quaternion(val x: Double, val y: Double, val z: Double, val w: Double)

    init {
        require(
            listOf(xMeters, yMeters, zMeters, yawDegrees, pitchDegrees, rollDegrees).all { it.isFinite() }
        ) { "Manual pose values must be finite numbers" }
        require(revision >= 0L) { "Manual pose revision cannot be negative" }
    }

    /** ZYX yaw-pitch-roll convention, returned as x/y/z/w to match the Linux pose schema. */
    fun orientationXyzw(): Quaternion {
        val yaw = yawDegrees * PI / 180.0
        val pitch = pitchDegrees * PI / 180.0
        val roll = rollDegrees * PI / 180.0
        val cy = cos(yaw * 0.5)
        val sy = sin(yaw * 0.5)
        val cp = cos(pitch * 0.5)
        val sp = sin(pitch * 0.5)
        val cr = cos(roll * 0.5)
        val sr = sin(roll * 0.5)
        return Quaternion(
            x = sr * cp * cy - cr * sp * sy,
            y = cr * sp * cy + sr * cp * sy,
            z = cr * cp * sy - sr * sp * cy,
            w = cr * cp * cy + sr * sp * sy
        )
    }

    fun summary(): String =
        "rev=$revision pos=($xMeters, $yMeters, $zMeters)m " +
            "yaw/pitch/roll=($yawDegrees, $pitchDegrees, $rollDegrees)deg"

    fun jsonFields(): Array<Pair<String, Any?>> {
        val q = orientationXyzw()
        return arrayOf(
            "android_pose_source" to "android_manual_input",
            "android_pose_frame_id" to "manual_map",
            "android_pose_revision" to revision,
            "android_pose_updated_elapsed_realtime_ms" to updatedAtDiagnosticMs,
            "android_position_x_m" to xMeters,
            "android_position_y_m" to yMeters,
            "android_position_z_m" to zMeters,
            "android_orientation_yaw_deg" to yawDegrees,
            "android_orientation_pitch_deg" to pitchDegrees,
            "android_orientation_roll_deg" to rollDegrees,
            "android_orientation_qx" to q.x,
            "android_orientation_qy" to q.y,
            "android_orientation_qz" to q.z,
            "android_orientation_qw" to q.w
        )
    }

    companion object {
        fun origin(): ManualPose = ManualPose(
            xMeters = 0.0,
            yMeters = 0.0,
            zMeters = 0.0,
            yawDegrees = 0.0,
            pitchDegrees = 0.0,
            rollDegrees = 0.0,
            revision = 0L,
            updatedAtDiagnosticMs = 0L
        )
    }
}
