package com.example.avtwinresponder

import kotlin.math.sqrt
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ManualPoseTest {
    @Test
    fun yawNinetyDegreesConvertsToXyzwQuaternion() {
        val pose = ManualPose(1.0, 2.0, 3.0, 90.0, 0.0, 0.0, 7L, 123L)
        val q = pose.orientationXyzw()
        assertEquals(0.0, q.x, 1e-12)
        assertEquals(0.0, q.y, 1e-12)
        assertEquals(sqrt(0.5), q.z, 1e-12)
        assertEquals(sqrt(0.5), q.w, 1e-12)
    }

    @Test
    fun exportedFieldsContainPositionOrientationAndRevision() {
        val pose = ManualPose(1.25, -2.5, 0.8, 10.0, 20.0, 30.0, 4L, 999L)
        val fields = pose.jsonFields().toMap()
        assertEquals("android_manual_input", fields["android_pose_source"])
        assertEquals(4L, fields["android_pose_revision"])
        assertEquals(1.25, fields["android_position_x_m"])
        assertEquals(20.0, fields["android_orientation_pitch_deg"])
        assertTrue(fields.keys.containsAll(listOf(
            "android_orientation_qx", "android_orientation_qy",
            "android_orientation_qz", "android_orientation_qw"
        )))
    }

    @Test(expected = IllegalArgumentException::class)
    fun nonFinitePoseIsRejected() {
        ManualPose(Double.NaN, 0.0, 0.0, 0.0, 0.0, 0.0, 1L, 0L)
    }
}
