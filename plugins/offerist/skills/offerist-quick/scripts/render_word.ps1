param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$proposalInput = (Resolve-Path -LiteralPath $InputPath).Path
$proposalOutput = [IO.Path]::GetFullPath($OutputPath)
if ([IO.Path]::GetExtension($proposalInput) -ne '.docx' -or [IO.Path]::GetExtension($proposalOutput) -ne '.pdf') {
    throw 'Expected a DOCX input and a PDF output.'
}
if (Test-Path -LiteralPath $proposalOutput) { throw 'PDF output must be new.' }
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($proposalOutput)))) { throw 'Output directory does not exist.' }
$proposalWord = $null
$proposalDocument = $null
$proposalOldBackground = $null
try {
    $proposalWord = New-Object -ComObject Word.Application
    $proposalWord.Visible = $false
    $proposalWord.DisplayAlerts = 0
    $proposalWord.AutomationSecurity = 3
    $proposalOldBackground = $proposalWord.Options.PrintBackgrounds
    $proposalWord.Options.PrintBackgrounds = $true
    $proposalDocument = $proposalWord.Documents.Open($proposalInput, $false, $true, $false)
    $proposalDocument.ExportAsFixedFormat($proposalOutput, 17)
    if (-not (Test-Path -LiteralPath $proposalOutput)) { throw 'Word did not create the PDF.' }
    [pscustomobject]@{input=$proposalInput; pdf=$proposalOutput; bytes=(Get-Item -LiteralPath $proposalOutput).Length} | ConvertTo-Json
} finally {
    if ($null -ne $proposalDocument) {
        $proposalDocument.Close(0)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($proposalDocument)
    }
    if ($null -ne $proposalWord) {
        if ($null -ne $proposalOldBackground) { $proposalWord.Options.PrintBackgrounds = $proposalOldBackground }
        $proposalWord.Quit(0)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($proposalWord)
    }
}
