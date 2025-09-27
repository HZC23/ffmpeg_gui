<#
.SYNOPSIS
    FFmpeg Toolbox Pro - Un outil en ligne de commande complet pour des opérations FFmpeg courantes.
.DESCRIPTION
    Ce script PowerShell fournit un menu interactif avancé pour manipuler des fichiers audio, vidéo et image via FFmpeg.
.NOTES
    Auteur: Gemini (Version améliorée)
    Version: 3.3 (Correction du problème de guillemets)
    Prérequis: FFmpeg doit être installé et accessible depuis le PATH du système.
#>

#region Fonctions utilitaires

$OutputEncoding = [System.Text.Encoding]::UTF8

function Test-FFmpeg {
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Host "❌ ERREUR : FFmpeg est introuvable..." -ForegroundColor Red
        exit
    }
    return $true
}

function Select-FileDialog {
    param([string]$Title = "Sélectionnez un fichier", [string]$Filter = "Tous les fichiers (*.*)|*.*")
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    if ($dialog.ShowDialog() -eq "OK") { return $dialog.FileName }
    return $null
}

function Select-FolderDialog {
    param([string]$Title = "Sélectionnez un dossier")
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Title
    if ($dialog.ShowDialog() -eq "OK") { return $dialog.SelectedPath }
    return $null
}

function Show-ChoiceMenu {
    param([string]$Title, [string[]]$Options, [string]$DefaultChoice = "1")
    Write-Host "`n$Title" -ForegroundColor White
    1..$Options.Count | ForEach-Object { Write-Host "  [$_] $($Options[$_-1])" }
    $choice = Read-Host "Votre choix"
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = $DefaultChoice }
    if ($choice -match '^\d+$' -and $choice -ge 1 -and $choice -le $Options.Length) { return $choice }
    Write-Host "Choix invalide." -ForegroundColor Red
    return $null
}

function Get-OutputFileName {
    param([string]$inputFile, [string]$suffix, [string]$extension)
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($inputFile)
    $defaultOutputFile = Join-Path -Path $PSScriptRoot -ChildPath "$($baseName)_$($suffix).$($extension)"
    $userOutputFile = Read-Host "Fichier de sortie proposé : `n$defaultOutputFile"
    if ([string]::IsNullOrWhiteSpace($userOutputFile)) { return $defaultOutputFile }
    return $userOutputFile
}

function Confirm-Overwrite {
    param([string]$filePath)
    if (Test-Path $filePath) {
        $choice = Read-Host "Le fichier existe. L'écraser ? (O/N)"
        return $choice -match '^[oO]'
    }
    return $true
}

function Run-FFmpegCommand {
    param([string[]]$arguments, [string]$outputFile)

    $quotedArgs = $arguments | ForEach-Object {
        if ($_ -match '\s') {
            "`"$_`""
        } else {
            $_
        }
    }

    Write-Host "`nCommande FFmpeg :" -ForegroundColor Cyan
    Write-Host "ffmpeg $($quotedArgs -join ' ')" -ForegroundColor White

    if ((Read-Host "`nLancer? (O/N)") -notmatch '^[oO]') { Write-Host "Annulé."; return $false }

    Write-Host "`n⏳ Traitement..." -ForegroundColor Green
    try {
        $process = Start-Process ffmpeg -ArgumentList $quotedArgs -Wait -NoNewWindow -PassThru -RedirectStandardError "$PSScriptRoot\ffmpeg_error.log"
        if ($process.ExitCode -ne 0) { Throw "Erreur FFmpeg (code $($process.ExitCode))." }
        Write-Host "`n✅ SUCCÈS." -ForegroundColor Green
        if ((Read-Host "Ouvrir le dossier? (O/N)") -match '^[oO]') { Invoke-Item (Split-Path $outputFile) }
        return $true
    } catch {
        Write-Host "`n❌ ERREUR." -ForegroundColor Red
        $log = "$PSScriptRoot\ffmpeg_error.log"
        if (Test-Path $log) {
            Write-Host "--- Log FFmpeg (voir $log) ---" -ForegroundColor Yellow
            Get-Content $log -TotalCount 20 | Write-Host -ForegroundColor Red
            Write-Host "---------------------------" -ForegroundColor Yellow
        }
        return $false
    }
}

#endregion

#region Fonctions de séquence d'images et vidéo

function Find-ImageSequences {
    param([string]$FolderPath)
    $imageExtensions = @('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.tga', '.dpx', '.exr')
    $files = Get-ChildItem -Path $FolderPath -File | Where-Object { $imageExtensions -contains $_.Extension.ToLower() }
    if ($files.Count -eq 0) { return $null }

    $sequences = $files | Group-Object { 
        $name = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
        $ext = $_.Extension
        $patternName = $name -replace '(\d+)$', '###'
        "$patternName$ext"
    }

    $detectedPatterns = @()
    foreach ($seq in $sequences) {
        if ($seq.Count -lt 2) { continue }
        $numbers = $seq.Group | ForEach-Object {
            $baseName = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
            $match = [regex]::Match($baseName, '(\d+)$')
            if ($match.Success) { [PSCustomObject]@{ String = $match.Groups[1].Value; Value = [int]$match.Groups[1].Value } }
        } | Sort-Object Value
        if ($numbers.Count -lt 2) { continue }

        $firstNumberStr = $numbers[0].String
        $padding = if ($firstNumberStr.StartsWith('0')) { $firstNumberStr.Length } else { 0 }
        $patternKey = $seq.Name
        $prefix = $patternKey.Split('###')[0]
        $extension = [System.IO.Path]::GetExtension($patternKey)
        $ffmpegPattern = if ($padding -gt 0) { "$($prefix)%0$($padding)d$($extension)" } else { "$($prefix)%d$($extension)" }

        $detectedPatterns += [PSCustomObject]@{
            Pattern = $ffmpegPattern
            Start = $numbers[0].Value
            Count = $seq.Count
            Description = "$ffmpegPattern ($($seq.Count) images, de $($numbers[0].Value) à $($numbers[-1].Value))"
        }
    }
    return $detectedPatterns
}

function Get-ImageSequencePattern {
    param([string]$sourceFolder)
    Write-Host "`n🔎 Recherche de séquences d'images..." -ForegroundColor Yellow
    $sequences = Find-ImageSequences -FolderPath $sourceFolder
    
    if (-not $sequences) {
        Write-Host "Aucune séquence automatique détectée." -ForegroundColor Yellow
        $imagePattern = Read-Host "`nEntrez le pattern des noms de fichiers (ex: img%04d.png)"
        $startNumberInput = Read-Host "`nEntrez le numéro de la première image (laissez vide si 0 ou 1)"
        if ($startNumberInput -match '^\d+$') { $startNumber = $startNumberInput }
    } else {
        $options = $sequences.Description + "Entrer le pattern manuellement"
        $choice = Show-ChoiceMenu -Title "Séquence(s) détectée(s), veuillez choisir :" -Options $options
        if (-not $choice) { return $null }
        if ($choice -le $sequences.Count) {
            $selectedSeq = $sequences[$choice - 1]
            $imagePattern = $selectedSeq.Pattern
            $startNumber = $selectedSeq.Start
        } else {
            $imagePattern = Read-Host "`nEntrez le pattern des noms de fichiers (ex: img%04d.png)"
            $startNumberInput = Read-Host "`nEntrez le numéro de la première image (laissez vide si 0 ou 1)"
            if ($startNumberInput -match '^\d+$') { $startNumber = $startNumberInput }
        }
    }

    if ([string]::IsNullOrWhiteSpace($imagePattern)) { return $null }
    return [PSCustomObject]@{ Pattern = $imagePattern; Start = $startNumber }
}

function Get-CommonVideoOptions {
    $fpsOptions = @("24 (Cinéma)", "25 (PAL)", "29.97 (NTSC)", "30", "50", "60 (Fluide)", "Personnalisé")
    $fpsChoice = Show-ChoiceMenu -Title "Choisissez les FPS :" -Options $fpsOptions -DefaultChoice 4
    if (-not $fpsChoice) { return $null }
    $fps = switch($fpsChoice){
        "1" {"24"}
        "2" {"25"}
        "3" {"29.97"}
        "4" {"30"}
        "5" {"50"}
        "6" {"60"}
        "7" { Read-Host "Entrez la valeur FPS souhaitée (ex: 15)" }
    }
    if (-not ($fps -match '^\d+([,.])\d+?$')) { Write-Host "Valeur FPS invalide." -ForegroundColor Red; return $null }

    $resOptions = @("Originale", "1080p (1920x1080)", "720p (1280x720)")
    $resChoice = Show-ChoiceMenu -Title "Choisissez la résolution de sortie :" -Options $resOptions
    if (-not $resChoice) { return $null }
    $resolutionFilter = switch($resChoice){
        "2" {"scale=-2:1080"}
        "3" {"scale=-2:720"}
        default {""}
    }
    return [PSCustomObject]@{ FPS = $fps; ResolutionFilter = $resolutionFilter }
}

#endregion

#region Fonctions principales

function Start-ExtractAudio {
    $inputFile = Select-FileDialog -Title "Vidéo pour extraction audio"
    if (-not $inputFile) { return }
    $audioFormat = Show-ChoiceMenu -Title "Format audio :" -Options @("MP3 (Standard)", "WAV (Non compressé)", "AAC (Bonne qualité)")
    if (-not $audioFormat) { return }
    $ext = @{"1"="mp3";"2"="wav";"3"="aac"}[$audioFormat]
    $outputFile = Get-OutputFileName -inputFile $inputFile -suffix "audio" -extension $ext
    if (-not $outputFile -or -not (Confirm-Overwrite $outputFile)) { return }
    $ffmpegArgs = @("-i", $inputFile, "-vn", "-acodec", @{"1"="libmp3lame";"2"="pcm_s16le";"3"="aac"}[$audioFormat], "-q:a", "2", $outputFile)
    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-ImagesToVideo {
    $sourceFolder = Select-FolderDialog -Title "Sélectionnez le dossier contenant la séquence d'images"
    if (-not $sourceFolder) { return }
    $sequenceInfo = Get-ImageSequencePattern -sourceFolder $sourceFolder
    if (-not $sequenceInfo) { Write-Host "Opération annulée."; return }
    $videoOptions = Get-CommonVideoOptions
    if (-not $videoOptions) { return }

    $outputFile = Get-OutputFileName -inputFile $sourceFolder -suffix "video" -extension "mp4"
    if (-not $outputFile -or -not (Confirm-Overwrite -filePath $outputFile)) { return }

    $inputPath = Join-Path -Path $sourceFolder -ChildPath $sequenceInfo.Pattern
    $ffmpegArgs = @("-framerate", $videoOptions.FPS)
    if ($sequenceInfo.Start) { $ffmpegArgs += "-start_number", $sequenceInfo.Start }
    $ffmpegArgs += "-i", $inputPath
    if ($videoOptions.ResolutionFilter) { $ffmpegArgs += "-vf", $videoOptions.ResolutionFilter }
    $ffmpegArgs += "-c:v", "libx264", "-pix_fmt", "yuv420p", $outputFile

    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-VideoToImages {
    $inputFile = Select-FileDialog -Title "Sélectionnez la vidéo à extraire"
    if (-not $inputFile) { return }

    $outputFolder = Select-FolderDialog -Title "Sélectionnez le dossier de sortie pour les images"
    if (-not $outputFolder) { return }

    $outputPattern = Read-Host "Entrez le pattern pour les noms de fichiers de sortie (ex: image-%04d.png)"
    if ([string]::IsNullOrWhiteSpace($outputPattern)) { $outputPattern = "image-%04d.png" }

    $outputFile = Join-Path -Path $outputFolder -ChildPath $outputPattern

    $ffmpegArgs = @("-i", $inputFile)

    $extractRange = Read-Host "Voulez-vous extraire une plage spécifique d'images ? (O/N)"
    if ($extractRange -match '^[oO]') {
        $startTime = Read-Host "Entrez le temps de départ (hh:mm:ss)"
        $frameCount = Read-Host "Entrez le nombre d'images à extraire"

        if (-not ([string]::IsNullOrWhiteSpace($startTime))) {
            $ffmpegArgs += "-ss", $startTime
        }
        if (-not ([string]::IsNullOrWhiteSpace($frameCount))) {
            $ffmpegArgs += "-vframes", $frameCount
        }
    }

    $ffmpegArgs += $outputFile

    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-MergeAudioVideo {
    $videoFile = Select-FileDialog -Title "Fichier vidéo"
    if (-not $videoFile) { return }
    $audioFile = Select-FileDialog -Title "Fichier audio"
    if (-not $audioFile) { return }
    $outputFile = Get-OutputFileName -inputFile $videoFile -suffix "merged" -extension "mp4"
    if (-not $outputFile -or -not (Confirm-Overwrite $outputFile)) { return }
    $ffmpegArgs = @("-i", $videoFile, "-i", $audioFile, "-c:v", "copy", "-c:a", "aac", "-shortest", $outputFile)
    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-TransparentVideo {
    $sourceFolder = Select-FolderDialog -Title "Sélectionnez le dossier des images PNG avec transparence"
    if (-not $sourceFolder) { return }
    $sequenceInfo = Get-ImageSequencePattern -sourceFolder $sourceFolder
    if (-not $sequenceInfo) { Write-Host "Opération annulée."; return }
    $videoOptions = Get-CommonVideoOptions
    if (-not $videoOptions) { return }

    $outputFile = Get-OutputFileName -inputFile $sourceFolder -suffix "transparent" -extension "webm"
    if (-not $outputFile -or -not (Confirm-Overwrite -filePath $outputFile)) { return }

    $inputPath = Join-Path -Path $sourceFolder -ChildPath $sequenceInfo.Pattern
    $ffmpegArgs = @("-framerate", $videoOptions.FPS)
    if ($sequenceInfo.Start) { $ffmpegArgs += "-start_number", $sequenceInfo.Start }
    $ffmpegArgs += "-i", $inputPath
    if ($videoOptions.ResolutionFilter) { $ffmpegArgs += "-vf", $videoOptions.ResolutionFilter }
    $ffmpegArgs += "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "2M", "-auto-alt-ref", "0", $outputFile

    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-ConvertVideo {
    $inputFile = Select-FileDialog -Title "Vidéo à convertir"
    if (-not $inputFile) { return }
    $format = Show-ChoiceMenu -Title "Format de sortie :" -Options @("MP4", "MKV", "MOV", "AVI", "GIF")
    if (-not $format) { return }
    $ext = @{"1"="mp4";"2"="mkv";"3"="mov";"4"="avi";"5"="gif"}[$format]
    $outputFile = Get-OutputFileName -inputFile $inputFile -suffix "converted" -extension $ext
    if (-not $outputFile -or -not (Confirm-Overwrite $outputFile)) { return }
    $ffmpegArgs = @("-i", $inputFile, $outputFile)
    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-AddSubtitles {
    $videoFile = Select-FileDialog -Title "Fichier vidéo"
    if (-not $videoFile) { return }
    $subtitleFile = Select-FileDialog -Title "Fichier de sous-titres" -Filter "Fichiers de sous-titres|*.srt;*.ass"
    if (-not $subtitleFile) { return }
    $outputFile = Get-OutputFileName -inputFile $videoFile -suffix "subtitled" -extension "mp4"
    if (-not $outputFile -or -not (Confirm-Overwrite $outputFile)) { return }
    $ffmpegArgs = @("-i", $videoFile, "-i", $subtitleFile, "-c", "copy", "-c:s", "mov_text", $outputFile)
    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

function Start-ChangeVideoSpeed {
    $inputFile = Select-FileDialog -Title "Vidéo à modifier"
    if (-not $inputFile) { return }
    $speed = Read-Host "Facteur de vitesse (ex: 2 pour 2x plus rapide, 0.5 pour 2x plus lent)"
    if (-not ($speed -match '^\d+([,.])\d+?$')) { Write-Host "Facteur invalide."; return }
    $outputFile = Get-OutputFileName -inputFile $inputFile -suffix "speed-$($speed)x" -extension "mp4"
    if (-not $outputFile -or -not (Confirm-Overwrite $outputFile)) { return }
    $ffmpegArgs = @("-i", $inputFile, "-filter:v", "setpts=$($speed)*PTS", "-filter:a", "atempo=$($speed)", $outputFile)
    Run-FFmpegCommand -arguments $ffmpegArgs -outputFile $outputFile
}

#endregion

#region Menu Principal

function Show-Menu {
    if (-not (Test-FFmpeg)) { return }
    do {
        Clear-Host
        $title = "FFmpeg Toolbox Pro v3.1"
        Write-Host ("=" * ($title.Length + 4)) -ForegroundColor Green
        Write-Host "  $title  " -ForegroundColor Green
        Write-Host ("=" * ($title.Length + 4)) -ForegroundColor Green; Write-Host
        
        Write-Host "--- Audio et Vidéo ---" -ForegroundColor Cyan
        Write-Host "   [1] Extraire l’audio"
        Write-Host "   [2] Créer une vidéo à partir d'images"
        Write-Host "   [3] Extraire les images d'une vidéo"
        Write-Host "   [4] Fusionner audio et vidéo"
        Write-Host "   [5] Créer une vidéo transparente"
        Write-Host "   [6] Convertir un format vidéo"
        Write-Host "   [7] Ajouter des sous-titres"
        Write-Host "   [8] Changer la vitesse"
        Write-Host
        
        Write-Host "   [Q] Quitter" -ForegroundColor Yellow
        Write-Host ("-" * ($title.Length + 4))
        
        $choice = Read-Host "Votre choix"

        switch ($choice) {
            "1" { Start-ExtractAudio }
            "2" { Start-ImagesToVideo }
            "3" { Start-VideoToImages }
            "4" { Start-MergeAudioVideo }
            "5" { Start-TransparentVideo }
            "6" { Start-ConvertVideo }
            "7" { Start-AddSubtitles }
            "8" { Start-ChangeVideoSpeed }
            "q" { break }
            default { Write-Host "Choix invalide."; Start-Sleep -Seconds 1 }
        }
        if ($choice -in 1..8) {
            Read-Host "`nAppuyez sur Entrée pour continuer..."
        }
    } while ($choice -ne 'q')
    
    Write-Host "`nAu revoir !"
}

Show-Menu

#endregion
