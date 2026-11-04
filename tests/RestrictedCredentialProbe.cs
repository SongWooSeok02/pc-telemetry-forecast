// A restricted token needs access from both its original SID set and BUILTIN\Users.
// This exercises a real kernel access check without creating a local account.
using System;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
public static class RestrictedCredentialProbe {
    [StructLayout(LayoutKind.Sequential)] struct SidAndAttributes {
        public IntPtr Sid; public uint Attributes;
    }
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool CreateRestrictedToken(IntPtr existing, uint flags, uint disabledCount,
        IntPtr disabled, uint privilegeCount, IntPtr privileges, uint restrictedCount,
        ref SidAndAttributes restricted, out SafeAccessTokenHandle token);
    public static void Check(string path, bool pipe) {
        using (var identity = WindowsIdentity.GetCurrent()) {
            var sid = new SecurityIdentifier("S-1-5-32-545");
            byte[] data = new byte[sid.BinaryLength]; sid.GetBinaryForm(data, 0);
            var memory = Marshal.AllocHGlobal(data.Length);
            try {
                Marshal.Copy(data, 0, memory, data.Length);
                var restriction = new SidAndAttributes { Sid=memory, Attributes=0 };
                SafeAccessTokenHandle token;
                if (!CreateRestrictedToken(identity.Token, 1, 0, IntPtr.Zero, 0, IntPtr.Zero,
                    1, ref restriction, out token))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                using (token) {
                    bool denied = false;
                    WindowsIdentity.RunImpersonated(token, () => {
                        try {
                            if (pipe) {
                                using (var client = new NamedPipeClientStream(".", "PcTelemetry.Registration.v1", PipeDirection.InOut))
                                    client.Connect(1000);
                            } else { File.ReadAllBytes(path); }
                        } catch (UnauthorizedAccessException) { denied = true; }
                    });
                    if (!denied) throw new Exception("Restricted identity unexpectedly allowed");
                }
            } finally { Marshal.FreeHGlobal(memory); }
        }
    }
}
