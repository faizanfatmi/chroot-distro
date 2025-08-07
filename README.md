# chroot-distro (WIP)

***chroot-distro***: Installs GNU/Linux distributions in a chroot environment on Android.  
- Based on [proot-distro](https://github.com/termux/proot-distro).

## Commands
Usage basics:
```
chroot-distro <command> <arguments>
```

Where `<command>` is a chroot-distro action command (see below to learn what
is available) and `<arguments>` is a list of options specific to given command.

Example of installing the distribution:
```
chroot-distro install debian
```

Some commands support aliases. For example, instead of
```
chroot-distro list
chroot-distro install debian
chroot-distro login debian
chroot-distro remove debian
```

you can type this:
```
chroot-distro ls
chroot-distro i debian
chroot-distro sh debian
chroot-distro rm debian
```

Command: `help`

This command will show the help information about `chroot-distro` usage.
* `chroot-distro help` - main page.
* `chroot-distro <command> --help` - view help for specific command.

### Listing distributions

Command: `list`

Aliases: `li`, `ls`

Shows a list of available distributions, their aliases, installation status
and comments.

### Start shell session

Command: `login`

Aliases: `sh`

Execute a shell within the given distribution. Example:
```
chroot-distro login debian
```

Execute a shell as specified user in the given distribution:
```
chroot-distro login --user admin debian
```

You can run a custom command as well:
```
chroot-distro login debian -- /usr/local/bin/mycommand --sample-option1
```

Argument `--` acts as terminator of `chroot-distro login` options processing.
All arguments behind it would not be treated as options of Chroot Distro.

Login command supports these behavior modifying options:
* `--user <username>`

  Use a custom login user instead of default `root`. You need to create the
  user via `useradd -U -m username` before using this option.

* `--termux-home`

  Mount Termux home directory as user home inside chroot environment.

* `--bind path:path`

  Create a custom file system path binding. Option expects argument in the
  given format:
  ```
  <host path>:<chroot path>
  ```
* `--work-dir`

  Set the working directory to given value. By default the working directory
  is same as user home.

### Uninstall distribution

Command: `remove`

Aliases: `rm`

This command completely deletes the installation of given system. Be careful
as it does not ask for confirmation. Deleted data is irrecoverably lost.

Usage example:
```
chroot-distro remove debian
```

### Clear downloads cache

Command: `clear-cache`

Aliases: `clear`, `cl`

This will remove all cached root file system archives.

## Acknowledgments:
Special thanks to:
- [proot-distro](https://github.com/termux/proot-distro)
- [Magisk-Modules-Alt-Repo/chroot-distro](https://github.com/Magisk-Modules-Alt-Repo/chroot-distro)
