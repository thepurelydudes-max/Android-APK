$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "WR Counter Assistant - Android signing key setup" -ForegroundColor Cyan
Write-Host "This creates the ONE permanent key that must sign every future APK." -ForegroundColor Yellow
Write-Host ""

$keytool = $null
if ($env:JAVA_HOME) {
    $candidate = Join-Path $env:JAVA_HOME "bin\\keytool.exe"
    if (Test-Path $candidate) {
        $keytool = $candidate
    }
}
if (-not $keytool) {
    $cmd = Get-Command keytool -ErrorAction SilentlyContinue
    if ($cmd) {
        $keytool = $cmd.Source
    }
}
if (-not $keytool) {
    throw "keytool not found. Install Java/JDK 17 or set JAVA_HOME, then run this script again."
}

$keystore = Join-Path $PSScriptRoot "WR_Counter_Assistant_release.jks"
if (Test-Path $keystore) {
    throw "Signing key already exists: $keystore. Do NOT overwrite it. Reuse the same file for all future APKs."
}

$secure = Read-Host "Create a password for the signing key (minimum 6 characters)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ([string]::IsNullOrWhiteSpace($password) -or $password.Length -lt 6) {
        throw "Password must contain at least 6 characters."
    }

    & $keytool -genkeypair -v `
        -keystore $keystore `
        -storetype JKS `
        -keyalg RSA `
        -keysize 2048 `
        -validity 10000 `
        -alias farliner `
        -storepass $password `
        -keypass $password `
        -dname "CN=FARLINER, OU=Apps, O=FARLINER"

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $keystore)) {
        throw "keytool failed to create the signing key."
    }

    Write-Host ""
    Write-Host "Signing certificate:" -ForegroundColor Cyan
    & $keytool -list -v -keystore $keystore -storepass $password -alias farliner |
        Select-String "SHA256:"

    $base64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($keystore))
    Set-Clipboard -Value $base64

    Write-Host ""
    Write-Host "DONE." -ForegroundColor Green
    Write-Host "The Base64 keystore has been copied to the Windows clipboard." -ForegroundColor Green
    Write-Host ""
    Write-Host "In GitHub -> Android-APK -> Settings -> Secrets and variables -> Actions:" -ForegroundColor Yellow
    Write-Host "1. Create secret WR_ANDROID_KEYSTORE_BASE64 and paste the clipboard value."
    Write-Host "2. Create secret WR_ANDROID_KEYSTORE_PASSWORD with the password you just entered."
    Write-Host ""
    Write-Host "IMPORTANT: back up this file somewhere private and never lose it:" -ForegroundColor Red
    Write-Host $keystore
    Write-Host "Do NOT commit it to GitHub. Without this exact key, future APKs cannot update installed copies."
}
finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
