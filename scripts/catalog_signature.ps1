param([Parameter(Mandatory=$true)][string]$Catalog)
$ErrorActionPreference='Stop'
$signature=Get-AuthenticodeSignature -LiteralPath $Catalog
[PSCustomObject]@{Path=$signature.Path;Status=[string]$signature.Status;StatusMessage=$signature.StatusMessage;Signer=if($signature.SignerCertificate){$signature.SignerCertificate.Subject}else{$null}} | ConvertTo-Json -Compress
