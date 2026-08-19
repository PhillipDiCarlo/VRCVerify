# Function to prompt for a version number
function GetVersion {
    $version = Read-Host "Please provide a version number"
    if (-not $version) {
        Write-Host "Version number is required. Exiting."
        exit 1
    }
    return $version
}

# Menu options
$menu = @"
1. Bot
2. VRC Online Checker
3. Dashboard (runs on the VPS, not the homelab)
4. VRChat Group Inviter
5. All
0. Exit
"@

Write-Host "Select an option to tag and upload:"
Write-Host $menu
$choice = Read-Host "Enter your choice (0-5)"

function TagAndPush($imageName, $version) {

    docker tag "$imageName" "italiandogs/vrcverify-${imageName}:$version"
    docker tag "$imageName" "italiandogs/vrcverify-${imageName}:latest"
    Write-Output "Tagged $imageName with $version and latest"

    docker push "italiandogs/vrcverify-${imageName}:$version"
    docker push "italiandogs/vrcverify-${imageName}:latest"
    Write-Output "Pushed $imageName with $version and latest"
}

function BuildDockerImages() {
    $buildChoice = @"
1. Yes
2. No
"@
    Write-Host "Would you like to build the Docker images before tagging and pushing?"
    Write-Host $buildChoice
    $build = Read-Host "Enter your choice (1-2)"
    
    switch ($build) {
        1 {
            Write-Host "Building Docker images..."
            docker-compose -f .\config\other_configs\docker-compose.yml build
            Write-Output "Build completed."
        }
        2 {
            Write-Host "Skipping build."
        }
        Default {
            Write-Host "Invalid choice. Skipping build."
        }
    }
}

# Ask if user wants to build before proceeding
$version = GetVersion
BuildDockerImages

# Switch case to handle the user's choice of Docker images to tag and push
switch ($choice) {
    1 {
        TagAndPush "discord-bot" $version
    }
    2 {
        TagAndPush "vrc-online-checker" $version
    }
    3 {
        TagAndPush "dashboard" $version
    }
    4 {
        TagAndPush "vrc-group-inviter" $version
    }
    5 {
        TagAndPush "discord-bot" $version
        TagAndPush "vrc-online-checker" $version
        TagAndPush "dashboard" $version
        TagAndPush "vrc-group-inviter" $version
    }
    0 {
        Write-Host "Exiting script."
        exit 0
    }
    Default {
        Write-Host "Invalid choice. Exiting script."
        exit 1
    }
}
