# Build the ROFL CUDA subset-sum miner (Windows).
# Requires the CUDA Toolkit (nvcc) and an NVIDIA GPU.
#
#   powershell -File gpu/build.ps1
#
# Produces gpu/rofl_gpu.dll, which miner.py loads automatically.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $here "subset_sum.cu"
$out = Join-Path $here "rofl_gpu.dll"

$nvcc = Get-Command nvcc -ErrorAction SilentlyContinue
if (-not $nvcc) {
    throw "nvcc not found. Install the CUDA Toolkit and make sure nvcc is on PATH."
}

function Find-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($vs) {
            $bat = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $bat) { return $bat }
        }
    }
    foreach ($root in @("C:\Program Files\Microsoft Visual Studio", "C:\Program Files (x86)\Microsoft Visual Studio", "E:\vs")) {
        $bat = Join-Path $root "VC\Auxiliary\Build\vcvars64.bat"
        if (Test-Path $bat) { return $bat }
    }
    return $null
}

$vcvars = Find-VcVars
if (-not $vcvars) {
    throw "cl.exe / vcvars64.bat not found. Install Visual Studio C++ build tools."
}

Write-Host "nvcc:   $($nvcc.Source)"
Write-Host "vcvars: $vcvars"
Write-Host "building $out"

# sm_86 is RTX 3070 Ti. 75/80/89 cover 20-series through 40-series.
# compute_86 PTX keeps the binary forward-compatible with newer cards.
$nvccCmd = @(
    "nvcc -allow-unsupported-compiler -D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH",
    "-O3 -std=c++17 --shared -cudart static",
    "-gencode=arch=compute_75,code=sm_75",
    "-gencode=arch=compute_80,code=sm_80",
    "-gencode=arch=compute_86,code=sm_86",
    "-gencode=arch=compute_86,code=compute_86",
    "-gencode=arch=compute_89,code=sm_89",
    '-Xcompiler "/MD"',
    "-o `"$out`"",
    "`"$src`""
) -join " "

cmd.exe /c "`"$vcvars`" >nul && $nvccCmd"
if ($LASTEXITCODE -ne 0) {
    throw "nvcc failed with exit code $LASTEXITCODE"
}

Write-Host "built $out"
Get-Item $out | Format-List Name, Length, LastWriteTime
