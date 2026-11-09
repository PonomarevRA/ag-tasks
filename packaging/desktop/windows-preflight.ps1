param([Parameter(Mandatory=$true)][string]$ApplicationRoot,
      [Parameter(Mandatory=$true)][string]$DataDirectory,
      [Parameter(Mandatory=$true)][string]$BackupDirectory,
      [Parameter(Mandatory=$true)][string]$ErrorFile)
$ErrorActionPreference = 'Stop'
try {
    $locations = @($ApplicationRoot, $DataDirectory, $BackupDirectory)
    foreach ($location in $locations) {
        if (-not [IO.Path]::IsPathRooted($location) -or $location -match '[\x00-\x1f]') { throw 'Choose absolute storage paths.' }
        $full = [IO.Path]::GetFullPath($location).TrimEnd('\')
        if ($full -ne $location.TrimEnd('\') -or $full.Length -le 3) { throw 'Choose canonical paths outside the drive root.' }
        $ancestor = $full
        while ($ancestor) {
            if (Test-Path -LiteralPath $ancestor) {
                $item = Get-Item -LiteralPath $ancestor -Force
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Junctions and symbolic links are not supported installation paths.' }
                if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Artifacts and backups must be outside source repositories.' }
            }
            $parent = [IO.Directory]::GetParent($ancestor)
            if (-not $parent) { break }
            $ancestor = $parent.FullName
        }
    }
    # Query exact executable paths, including the application's backend children.
    $prefix = [IO.Path]::GetFullPath($ApplicationRoot).TrimEnd('\') + '\'
    foreach ($process in (Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ($process.ExecutablePath -and $process.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Quit the application and its MCP processes before updating. Closing a window may keep it running in the tray.'
        }
    }
    exit 0
} catch {
    [IO.File]::WriteAllText($ErrorFile, $_.Exception.Message)
    exit 1
}
