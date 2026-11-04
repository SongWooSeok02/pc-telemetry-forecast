# Only writes ephemeral test keys/certificates to the caller's private temp directory.
# Does not open or modify any system/user certificate trust store.
param([Parameter(Mandatory)][string]$Directory)
$ErrorActionPreference='Stop'
$rsa=[Security.Cryptography.RSA]::Create(2048)
$serverKey=[Security.Cryptography.RSA]::Create(2048)
$authority=$null; $certificate=$null
try {
    $request=[Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=S203 ephemeral CA',
        $rsa,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true,$false,0,$true))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new('KeyCertSign,CrlSign',$true))
    $authority=$request.CreateSelfSigned([DateTimeOffset]::Now.AddMinutes(-5),[DateTimeOffset]::Now.AddHours(1))
    $request=[Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=localhost',
        $serverKey,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $san=[Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
    $san.AddDnsName('localhost'); $san.AddIpAddress([Net.IPAddress]::Loopback)
    $request.CertificateExtensions.Add($san.Build())
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false,$false,0,$true))
    $usages=[Security.Cryptography.OidCollection]::new()
    $null=$usages.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.1'))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usages,$true))
    $certificate=$request.Create($authority,[DateTimeOffset]::Now.AddMinutes(-2),
        [DateTimeOffset]::Now.AddMinutes(30),[Security.Cryptography.RandomNumberGenerator]::GetBytes(16))
    [IO.File]::WriteAllText((Join-Path $Directory 'server.pem'),$certificate.ExportCertificatePem()+"`n"+$authority.ExportCertificatePem())
    [IO.File]::WriteAllText((Join-Path $Directory 'ca.pem'),$authority.ExportCertificatePem())
    [IO.File]::WriteAllText((Join-Path $Directory 'server.key'),$serverKey.ExportPkcs8PrivateKeyPem())
} finally {
    if ($certificate) { $certificate.Dispose() }
    if ($authority) { $authority.Dispose() }
    $rsa.Dispose(); $serverKey.Dispose()
}
