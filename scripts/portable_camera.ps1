param([ValidateSet('List','Import')][string]$Mode='List',[string]$DeviceKey,[string]$Destination)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding
$shell=New-Object -ComObject Shell.Application
$roots=@($shell.NameSpace(17).Items() | Where-Object { -not $_.IsFileSystem -and $_.Path -match 'usb|wpd|mtp|ptp' })
if($Mode -eq 'List'){
    @{devices=@($roots | ForEach-Object { @{name=$_.Name;key=$_.Path} })} | ConvertTo-Json -Depth 5 -Compress
    exit
}
$device=$roots | Where-Object {$_.Path -eq $DeviceKey} | Select-Object -First 1
if(-not $device){throw 'The selected portable camera is not present. Connect/unlock it, choose PTP/MTP mode and scan again.'}
$destinationFull=[IO.Path]::GetFullPath($Destination)
if(-not $destinationFull.StartsWith('D:\',[StringComparison]::OrdinalIgnoreCase)){throw 'Portable camera output must be on D:.'}
[IO.Directory]::CreateDirectory($destinationFull) | Out-Null
$files=New-Object System.Collections.ArrayList
$failures=New-Object System.Collections.ArrayList
$extensions='\.(jpg|jpeg|jpe|png|tif|tiff|bmp|gif|heic|heif|avif|dng|raw|arw|srf|sr2|cr2|cr3|crw|nef|nrw|raf|orf|rw2|pef|srw|mpg|mpeg|mp4|mov|m4v|avi|mts|m2ts|m2t|mxf|mod|tod|vro|vob|3gp|dv|asf|wmv|mkv|xml|thm|moi|cpi|mpl|mpls|clpi|xmp|lrv|srt)$'
function Read-Items($folder,[string]$relative,[int]$depth){
    if($depth -gt 64 -or $files.Count -gt 100000){throw 'Portable device directory limit reached. Use Windows to import a smaller folder.'}
    foreach($item in $folder.Items()){
        $name=[string]$item.Name
        if([IO.Path]::GetFileName($name) -ne $name -or $name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0){$failures.Add(@{name=$name;error='Unsupported or unsafe device filename.'})|Out-Null;continue}
        $child=if($relative){Join-Path $relative $name}else{$name}
        if($item.IsFolder){Read-Items $item.GetFolder $child ($depth+1)}
        elseif($name -match $extensions){
            $size=[long]$item.ExtendedProperty('System.Size')
            if($size -le 0){$size=[long]$item.Size}
            if($size -le 0){$failures.Add(@{name=$child;error='The device did not report a nonzero file size. Use Windows manual import for this item.'})|Out-Null;continue}
            $files.Add(@{item=$item;relative=$child;bytes=$size})|Out-Null
        }
    }
}
Read-Items $device.GetFolder '' 0
$copied=New-Object System.Collections.ArrayList
$total=[long](($files | Measure-Object -Property bytes -Sum).Sum)
$done=[long]0
$reportPath=Join-Path $destinationFull 'portable_report.json'
foreach($file in $files){
    $target=Join-Path $destinationFull $file.relative
    $targetFull=[IO.Path]::GetFullPath($target)
    if(-not $targetFull.StartsWith($destinationFull+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe portable-device output path.'}
    $parent=Split-Path -Parent $targetFull;[IO.Directory]::CreateDirectory($parent)|Out-Null
    try{
        $shell.NameSpace($parent).CopyHere($file.item,1556)
        $deadline=[DateTime]::UtcNow.AddSeconds([Math]::Max(180,$file.bytes/1048576*5))
        $ready=$false
        while([DateTime]::UtcNow -lt $deadline){
            if([IO.File]::Exists($targetFull) -and (Get-Item -LiteralPath $targetFull).Length -eq $file.bytes){
                try{$stream=[IO.File]::Open($targetFull,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None);$stream.Dispose();$ready=$true;break}catch{}
            }
            Start-Sleep -Milliseconds 500
        }
        if(-not $ready){throw 'Windows did not finish a size-verifiable copy before the timeout.'}
        $copied.Add(@{relative=$file.relative;target=$targetFull;bytes=$file.bytes;size_verified=$true})|Out-Null
        $done+=$file.bytes
    }catch{$failures.Add(@{name=$file.relative;error=$_.Exception.Message})|Out-Null}
    @{device=$device.Name;files=@($copied);failures=@($failures);done_bytes=$done;total_bytes=$total;source_validation='Device-reported size; source checksum is unavailable through this Windows Shell bridge.'} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}
@{device=$device.Name;files=@($copied);failures=@($failures);done_bytes=$done;total_bytes=$total;source_validation='Device-reported size; source checksum is unavailable through this Windows Shell bridge.'} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8
@{copied=$copied.Count;failed=$failures.Count;report=$reportPath} | ConvertTo-Json -Compress
