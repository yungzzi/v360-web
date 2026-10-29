package com.p2p.pppp_api;

/** Exact JNI names/descriptors from the user's APK; no credentials are compiled in. */
public final class PPCS_APIs {
    static { System.loadLibrary("PPCS_API"); }
    public static native int PPCS_Initialize(byte[] parameters);
    public static native int PPCS_DeInitialize();
    public static native int PPCS_GetAPIVersion();
    public static native String PPCS_GetAPIInformation();
    public static native int PPCS_Connect(String did, byte mode, int udpPort);
    public static native int PPCS_ConnectByServer(String did, byte mode, int udpPort, String server);
    public static native int PPCS_Connect_Break();
    public static native int PPCS_ForceClose(int session);
    public static native int PPCS_Read(int session, byte channel, byte[] bytes, int[] size, int timeout);
    public static native int PPCS_Write(int session, byte channel, byte[] bytes, int size);
    private PPCS_APIs() { }
}
