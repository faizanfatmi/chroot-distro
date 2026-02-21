#!/usr/bin/env bash
# check_distro_versions.sh — Fetch latest upstream distro versions and compare
# with local dist_version values in distro-build/*.sh scripts.
#
# Usage:
#   ./check_distro_versions.sh --check                   # Output JSON of available updates
#   ./check_distro_versions.sh --update NAME VER [CODENAME] # Update dist_version (and optionally dist_codename) in a script

set -uo pipefail

DISTRO_BUILD_DIR="distro-build"

get_local_version() {
	grep -oP 'dist_version="\K[^"]+' "$1" 2>/dev/null || true
}

update_file() {
	sed -i "s/dist_version=\"[^\"]*\"/dist_version=\"${2}\"/" "$1"
	echo "Updated $1 to version $2"
	# If a codename was provided, update dist_codename too
	if [ -n "${3:-}" ]; then
		sed -i "s/dist_codename=\"[^\"]*\"/dist_codename=\"${3}\"/" "$1"
		echo "Updated $1 codename to $3"
	fi
}

fetch_url() {
	curl -fsSL --max-time 30 "$1" 2>/dev/null
}

fetch_alpine() {
	local page
	page=$(fetch_url "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/aarch64/") || return 0
	echo "$page" | grep -oP 'alpine-minirootfs-\K[0-9]+\.[0-9]+\.[0-9]+' | sort -Vr | head -1
}

fetch_archlinux() {
	local page
	page=$(fetch_url "https://mirror.rackspace.com/archlinux/iso/") || return 0
	echo "$page" | grep -oP '[0-9]{4}\.[0-9]{2}\.[0-9]{2}(?=/)' | sort -Vr | head -1
}

fetch_fedora() {
	local page major container_page
	page=$(fetch_url "https://mirror.de.leaseweb.net/fedora/linux/releases/") || return 0
	major=$(echo "$page" | grep -oP 'href="\K[0-9]+(?=/)' | sort -nr | head -1)
	[ -z "$major" ] && return 0

	container_page=$(fetch_url "https://mirror.de.leaseweb.net/fedora/linux/releases/${major}/Container/aarch64/images/") || return 0
	echo "$container_page" | grep -oP "Fedora-Container-Base-Generic-\K${major}-[0-9]+\.[0-9]+" | sort -Vr | head -1
}

fetch_kali() {
	local page
	page=$(fetch_url "http://cdimage.kali.org/current/") || return 0
	echo "$page" | grep -oP 'kali-linux-\K[0-9]+\.[0-9]+[a-z]?(?=-installer)' | head -1
}

fetch_manjaro() {
	local response
	response=$(fetch_url "https://api.github.com/repos/manjaro-arm/rootfs/releases/latest") || return 0
	echo "$response" | jq -r '.tag_name // empty' 2>/dev/null
}

fetch_opensuse() {
	# Docker Hub tags API. Filter for modern Leap versions (15.x+) only.
	local response
	response=$(fetch_url "https://registry.hub.docker.com/v2/repositories/opensuse/leap/tags/?page_size=100&ordering=last_updated") || return 0
	# Exclude legacy 42.x line (EOL since 2018); only keep modern Leap 15.x+
	echo "$response" | jq -r '.results[].name' 2>/dev/null |
		grep -P '^[0-9]+\.[0-9]+$' |
		grep -v '^42\.' |
		sort -Vr | head -1
}

fetch_rockylinux() {
	# Rocky Linux directory listing has versioned directories like 10.0/, 10.1/, etc.
	# We directly parse the highest X.Y version from the listing.
	local page
	page=$(fetch_url "https://download.rockylinux.org/pub/rocky/") || return 0
	echo "$page" | grep -oP 'href="\K[0-9]+\.[0-9]+(?=/")' | sort -Vr | head -1
}

fetch_void() {
	# Void Linux live directory has YYYYMMDD/ directories.
	local page
	page=$(fetch_url "https://repo-default.voidlinux.org/live/") || return 0
	echo "$page" | grep -oP '[0-9]{8}(?=/)' | sort -nr | head -1
}

fetch_debian() {
	# Parse the Debian stable Release file for Version and Codename.
	local release_info version codename
	release_info=$(fetch_url "https://deb.debian.org/debian/dists/stable/Release") || return 0
	version=$(echo "$release_info" | grep -oP '^Version:\s*\K\S+' | head -1)
	codename=$(echo "$release_info" | grep -oP '^Codename:\s*\K\S+' | head -1)
	[ -n "$version" ] && [ -n "$codename" ] && echo "${version}|${codename}"
}

fetch_ubuntu() {
	# Parse Ubuntu meta-release for the latest supported version.
	local meta
	meta=$(fetch_url "http://changelogs.ubuntu.com/meta-release") || return 0
	# The file has blocks with Dist:/Version:/Supported: fields.
	# We find the last entry where Supported: 1.
	local last_dist="" last_version=""
	local cur_dist="" cur_version="" cur_supported=""
	while IFS= read -r line; do
		case "$line" in
		Dist:*) cur_dist=$(echo "$line" | sed 's/^Dist:\s*//') ;;
		Version:*) cur_version=$(echo "$line" | sed 's/^Version:\s*//' | grep -oP '^[0-9]+\.[0-9]+') ;;
		Supported:*)
			cur_supported=$(echo "$line" | sed 's/^Supported:\s*//')
			if [ "$cur_supported" = "1" ] && [ -n "$cur_dist" ] && [ -n "$cur_version" ]; then
				last_dist="$cur_dist"
				last_version="$cur_version"
			fi
			;;
		esac
	done <<<"$meta"
	[ -n "$last_version" ] && [ -n "$last_dist" ] && echo "${last_version}|${last_dist}"
}

fetch_trisquel() {
	# Parse Trisquel archive dists directory for codenames, then check
	# each Release file for Version to find the latest.
	local page
	page=$(fetch_url "https://archive.trisquel.org/trisquel/dists/") || return 0
	# Get base codenames (exclude -updates, -security, -backports, sugar-*)
	# Only consider codenames that were modified in the last 2 years (recent entries)
	local codenames
	codenames=$(echo "$page" | grep -oP 'href="\K[a-z]+(?=/")' |
		grep -v 'updates' | grep -v 'security' | grep -v 'backports' | grep -v 'sugar' |
		sort -u)
	[ -z "$codenames" ] && return 0

	local best_version="" best_codename=""
	for codename in $codenames; do
		local release_info version
		release_info=$(fetch_url "https://archive.trisquel.org/trisquel/dists/${codename}/Release") || continue
		version=$(echo "$release_info" | grep -oP '^Version:\s*\K\S+' | head -1)
		if [ -n "$version" ]; then
			if [ -z "$best_version" ] || [ "$(printf '%s\n%s' "$best_version" "$version" | sort -V | tail -1)" = "$version" ]; then
				best_version="$version"
				best_codename="$codename"
			fi
		fi
	done
	[ -n "$best_version" ] && [ -n "$best_codename" ] && echo "${best_version}|${best_codename}"
}

check_updates() {
	if [ ! -d "$DISTRO_BUILD_DIR" ]; then
		echo "Error: $DISTRO_BUILD_DIR not found." >&2
		exit 1
	fi

	local json_entries=()

	for file in "$DISTRO_BUILD_DIR"/*.sh; do
		local distro_name local_version upstream_version
		distro_name=$(basename "$file" .sh)
		local_version=$(get_local_version "$file")

		if [ -z "$local_version" ]; then
			echo "Warning: Could not find dist_version in $file" >&2
			continue
		fi

		upstream_version=""

		case "$distro_name" in
		alpine) upstream_version=$(fetch_alpine) ;;
		archlinux) upstream_version=$(fetch_archlinux) ;;
		fedora) upstream_version=$(fetch_fedora) ;;
		kali) upstream_version=$(fetch_kali) ;;
		manjaro) upstream_version=$(fetch_manjaro) ;;
		opensuse) upstream_version=$(fetch_opensuse) ;;
		rockylinux) upstream_version=$(fetch_rockylinux) ;;
		void) upstream_version=$(fetch_void) ;;
		debian) upstream_version=$(fetch_debian) ;;
		ubuntu) upstream_version=$(fetch_ubuntu) ;;
		trisquel) upstream_version=$(fetch_trisquel) ;;
		*)
			echo "Warning: No upstream checker for $distro_name, skipping" >&2
			continue
			;;
		esac

		if [ -z "$upstream_version" ]; then
			echo "Warning: Could not fetch upstream version for $distro_name" >&2
			continue
		fi

		local new_codename=""
		if [[ "$upstream_version" == *"|"* ]]; then
			new_codename="${upstream_version#*|}"
			upstream_version="${upstream_version%%|*}"
		fi

		if [ "$upstream_version" != "$local_version" ]; then
			local entry="{\"name\":\"${distro_name}\",\"current\":\"${local_version}\",\"new\":\"${upstream_version}\",\"file\":\"${file}\""
			if [ -n "$new_codename" ]; then
				entry+=",\"new_codename\":\"${new_codename}\""
			fi
			entry+="}"
			json_entries+=("$entry")
		else
			echo "Info: $distro_name is up-to-date ($local_version)" >&2
		fi
	done

	if [ ${#json_entries[@]} -eq 0 ]; then
		echo "[]"
	else
		local json="["
		for i in "${!json_entries[@]}"; do
			[ "$i" -gt 0 ] && json+=","
			json+="${json_entries[$i]}"
		done
		json+="]"
		echo "$json" | jq '.'
	fi
}

case "${1:-}" in
--check)
	check_updates
	;;
--update)
	if [ $# -lt 3 ]; then
		echo "Usage: $0 --update DISTRO VERSION [CODENAME]" >&2
		exit 1
	fi
	file_path="${DISTRO_BUILD_DIR}/${2}.sh"
	if [ ! -f "$file_path" ]; then
		echo "Error: $file_path does not exist." >&2
		exit 1
	fi
	update_file "$file_path" "$3" "${4:-}"
	;;
*)
	echo "Usage: $0 --check | --update DISTRO VERSION [CODENAME]" >&2
	exit 1
	;;
esac
