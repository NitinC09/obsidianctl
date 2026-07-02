def _detect_chroot_cmd():
    import shutil, subprocess

    def _read_os_release():
        vals = {}
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, _, v = line.partition("=")
                            vals[k.strip()] = v.strip().strip('"')
            except FileNotFoundError:
                continue
        return vals

    os_release = _read_os_release()
    distro_id = os_release.get("ID", "").lower()
    distro_id_like = os_release.get("ID_LIKE", "").lower()

    # Arch and anything derived from it (Manjaro, EndeavourOS, etc.)
    is_arch_like = "arch" in distro_id or "arch" in distro_id_like

    if is_arch_like and shutil.which("arch-chroot"):
        def do_chroot(mount_dir, *extra_args, check=True):
            cmd = f"arch-chroot {mount_dir}"
            if extra_args:
                cmd += " " + " ".join(extra_args)
            run_command(cmd, check=check)
        return do_chroot
    else:
        # Generic chroot: manually bind-mount proc/sys/dev, run command, unmount
        def do_chroot(mount_dir, *extra_args, check=True):
            import subprocess
            mounts = [
                f"mount -t proc /proc {mount_dir}/proc",
                f"mount --rbind /sys {mount_dir}/sys",
                f"mount --make-rslave {mount_dir}/sys",
                f"mount --rbind /dev {mount_dir}/dev",
                f"mount --make-rslave {mount_dir}/dev",
            ]
            for m in mounts:
                run_command(m, check=False)
            cmd = f"chroot {mount_dir}"
            if extra_args:
                cmd += " " + " ".join(extra_args)
            run_command(cmd, check=check)
            run_command(f"umount -R {mount_dir}/proc {mount_dir}/sys {mount_dir}/dev", check=False)
        return do_chroot

# Detect once at module load time so all handle_* functions share the same instance
_chroot = _detect_chroot_cmd()


def _is_openrc_at(mount_dir):
    """True if the rootfs at mount_dir uses OpenRC (openrc-init binary present)."""
    return os.path.exists(f"{mount_dir}/sbin/openrc-init")


def _with_temp_mount(part_path, mount_point, body, cleanup_dir=False):
    """Mount part_path at mount_point, run body() with the mount in place, then unmount.

    body is a callable that takes no arguments. Errors during body still trigger
    cleanup. cleanup_dir=True removes the mount directory after unmount
    (use for one-shot scratch dirs; leave False for dirs that should persist).
    """
    run_command(f"mkdir -p {mount_point}")
    try:
        run_command(f"mount {part_path} {mount_point}")
        body()
    finally:
        run_command(f"umount {mount_point}", check=False)
        if cleanup_dir:
            run_command(f"rmdir {mount_point}", check=False)


def _mount_slot_chroot(mount_dir, device, slot):
    """Mount the 5-partition layout (root, ESP, etc_ab, var_ab, home_ab) for a slot.

    slot is 'a' or 'b'. The caller is responsible for unmounting (usually
    `umount -R {mount_dir}`).
    """
    root_label = f"root_{slot}"
    esp_label = "ESP_A" if slot == "a" else "ESP_B"
    mounts = [
        f"mount {lordo(root_label, device)} {mount_dir}/",
        f"mount {lordo(esp_label, device)} {mount_dir}/efi",
        f"mount {lordo('etc_ab', device)} {mount_dir}/run/etc_ab --mkdir",
        f"mount {lordo('var_ab', device)} {mount_dir}/var",
        f"mount {lordo('home_ab', device)} {mount_dir}/home",
    ]
    for cmd in mounts:
        run_command(cmd)


def _write_fstab(path, device, fstype, slot):
    """Write an fstab for the given slot ('a' or 'b') to path."""
    root_label = f"root_{slot}"
    esp_label = "ESP_A" if slot == "a" else "ESP_B"
    content = f"""\
{lordo(root_label, device)}  /             {fstype}  defaults,noatime 0 1
{lordo(esp_label, device)}   /efi          vfat      defaults,noatime 0 2
{lordo('etc_ab', device)}    /run/etc_ab   {fstype}  defaults,noatime 0 2
{lordo('var_ab', device)}    /var          {fstype}  defaults,noatime 0 2
{lordo('home_ab', device)}   /home         {fstype}  defaults,noatime 0 2
"""
    if not os.path.exists(os.path.dirname(path)):
        run_command(f"mkdir -p {os.path.dirname(path)}")
    with open(path, "w") as f:
        f.write(content)


def _install_grub_to_slot(mount_dir, device, slot, use_grub2):
    """Install GRUB to the given slot. Slot must be 'a' or 'b'.

    Mounts the slot's partitions, runs grub-install + grub-mkconfig, unmounts.
    Sets OpenRC kernel cmdline if the slot uses OpenRC.
    """
    bootloader_id = f"ObsidianOSslot{slot.upper()}"
    _mount_slot_chroot(mount_dir, device, slot)
    try:
        grub_cmd = "grub2-install" if use_grub2 else "grub-install"
        mkconfig_cmd = "grub2-mkconfig" if use_grub2 else "grub-mkconfig"
        _chroot(mount_dir, f"{grub_cmd} --target=x86_64-efi --efi-directory=/efi --bootloader-id={bootloader_id}")
        # Only slot A handles os-prober config (slot B inherits from rsync).
        if slot == "a":
            _chroot(mount_dir, "sed -i 's|^#*GRUB_DISABLE_OS_PROBER=.*|GRUB_DISABLE_OS_PROBER=false|' /etc/default/grub")
        # OpenRC needs init= explicitly on the kernel cmdline.
        if not use_grub2 and _is_openrc_at(mount_dir):
            run_command(
                f"sed -i 's|^#*GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"init=/sbin/openrc-init\"|' "
                f"{mount_dir}/etc/default/grub"
            )
        # grub-mkconfig writes to /boot/grub/grub.cfg on the rootfs. The ESP
        # only needs the EFI binary (placed by grub-install above); the real
        # grub.cfg lives on root and is found via UUID at boot.
        run_command(f"umount {mount_dir}/efi")
        if use_grub2:
            run_command(f"mkdir -p {mount_dir}/efi/grub")
        else:
            run_command(f"mkdir -p {mount_dir}/boot/grub")
        _chroot(mount_dir, f"{mkconfig_cmd} -o /boot/grub/grub.cfg")
    finally:
        run_command(f"umount -R {mount_dir}", check=False)


def _install_systemdboot_to_esp(esp_part, slot_letter):
    """Install systemd-boot's EFI binary to the given ESP partition."""
    mount_point = f"/mnt/obsidian_esp_{slot_letter}"
    def _do():
        run_command(
            f'bootctl --esp-path={mount_point} '
            f'--efi-boot-option-description="ObsidianOS (Slot {slot_letter.upper()})" install'
        )
    _with_temp_mount(esp_part, mount_point, _do, cleanup_dir=True)


def _write_systemdboot_config_to_esp(esp_part, slot_letter, loader_conf, entry_a, entry_b):
    """Write loader.conf and both slot entries to a given ESP."""
    mount_point = f"/mnt/obsidian_esp_{slot_letter}_config"
    def _do():
        run_command(f"mkdir -p {mount_point}/loader/entries")
        with open(f"{mount_point}/loader/loader.conf", "w") as f:
            f.write(loader_conf)
        with open(f"{mount_point}/loader/entries/obsidian-a.conf", "w") as f:
            f.write(entry_a)
        with open(f"{mount_point}/loader/entries/obsidian-b.conf", "w") as f:
            f.write(entry_b)
    _with_temp_mount(esp_part, mount_point, _do, cleanup_dir=True)


def handle_mkobsidiansfs(args):
    _, ext = os.path.splitext(args.system_sfs)
    is_gentoo = ext == ".mkobsfs-gentoo"
    script_name = "mkobsidiansfs-gentoo" if is_gentoo else "mkobsidiansfs"
    repo_url = "https://github.com/Obsidian-OS/mkobsidiansfs/"
    tmp_dir = "/tmp/mkobsidiansfs"
    tmp_script = f"{tmp_dir}/{script_name}"

    out_sfs = "/tmp/tmp_system.sfs" if is_gentoo else "tmp_system.sfs"
    if shutil.which(script_name):
        os.system(f"{script_name} {args.system_sfs} {out_sfs}")
    else:
        if shutil.which("git"):
            os.system(
                f"git clone {repo_url} {tmp_dir};"
                f"chmod u+x {tmp_script};"
                f"{tmp_script} {args.system_sfs} {out_sfs}"
            )
        else:
            print(
                "No git or mkobsidiansfs found. Please install one of these to directly pass in an .mkobsfs."
            )
            sys.exit(1)
    args.system_sfs = out_sfs
    handle_install(args)
    os.remove(out_sfs)


def handle_install(args):
    checkroot()
    device = args.device
    system_sfs = args.system_sfs or "/etc/system.sfs"
    _, ext = os.path.splitext(system_sfs)
    if ext in (".mkobsfs", ".mkobsfs-gentoo"):
        handle_mkobsidiansfs(args)
        sys.exit()
    if args.dual_boot:
        handle_dual_boot(args)
        return

    if not os.path.exists(device):
        print(f"Error: Device '{device}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(system_sfs):
        print(f"Error: System image '{system_sfs}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"WARNING: This will destroy all data on {device}.")
    confirm = input("Are you sure you want to proceed? (y/N): ")
    if confirm.lower() != "y":
        print("Installation aborted.")
        sys.exit(0)
    fstype="ext4"
    if args.use_f2fs:
        print(f"WARNING: F2FS is a filesystem ONLY for fragile NAND.")
        confirm = input("This is only for advanced users. Are you sure you want to proceed? (y/N): ")
        if confirm.lower() != "y":
            fstype="ext4"
        else:
            fstype="f2fs"
    print("Partitioning device...")
    partition_table = f"""
label: gpt
,{args.esp_size},U,*
,{args.esp_size},U,*
,{args.rootfs_size},L,*
,{args.rootfs_size},L,*
,{args.etc_size},L,*
,{args.var_size},L,*
,,L,*
"""
    run_command(f"sfdisk {device}", input=partition_table, text=True)
    run_command("partprobe", check=False)
    print("Waiting for device partitions to settle...")
    run_command("udevadm settle")
    part1, part2, part3, part4, part5, part6, part7 = (
        _get_part_path(device, 1),
        _get_part_path(device, 2),
        _get_part_path(device, 3),
        _get_part_path(device, 4),
        _get_part_path(device, 5),
        _get_part_path(device, 6),
        _get_part_path(device, 7),
    )

    print("Formatting partitions...")
    format_commands = [
        f"mkfs.fat    -F32 -n ESP_A   {part1}",
        f"mkfs.fat    -F32 -n ESP_B   {part2}",
        f"mkfs.{fstype} -F -L root_a  {part3}",
        f"mkfs.{fstype} -F -L root_b  {part4}",
        f"mkfs.{fstype} -F -L etc_ab  {part5}",
        f"mkfs.{fstype} -F -L var_ab  {part6}",
        f"mkfs.{fstype} -F -L home_ab {part7}",
    ]
    for cmd in format_commands:
        run_command(cmd)

    # Wait for partitions to settle after formatting
    run_command("partprobe", check=False)
    run_command("udevadm settle")

    mount_dir = "/mnt/obsidian_install"
    run_command(f"mkdir -p {mount_dir}")
    print("Mounting root partition for slot 'a'...")
    run_command(f"mount {lordo('root_a', device)} {mount_dir}")
    print(f"Extracting system from {system_sfs} to slot 'a'...")
    run_command(f"unsquashfs -f -d {mount_dir} -no-xattrs {system_sfs}")
    run_command(f"mount --bind {mount_dir} {mount_dir}")
    print("Generating fstab for slot 'a'...")
    # On OpenRC, /run is cleared at boot so /run/etc_ab needs to be created
    # before localmount processes fstab.
    if _is_openrc_at(mount_dir):
        os.makedirs(f"{mount_dir}/etc/init.d", exist_ok=True)
        with open(f"{mount_dir}/etc/init.d/obsidian-mkmountpoints", "w") as _f:
            _f.write(
                "#!/sbin/openrc-run\n"
                "description=\"Create ObsidianOS mount points in /run\"\n"
                "depend() {\n"
                "    before localmount\n"
                "    keyword -prefix\n"
                "}\n"
                "start() {\n"
                "    mkdir -p /run/etc_ab\n"
                "}\n"
            )
        os.chmod(f"{mount_dir}/etc/init.d/obsidian-mkmountpoints", 0o755)
        os.makedirs(f"{mount_dir}/etc/runlevels/sysinit", exist_ok=True)
        _dst = f"{mount_dir}/etc/runlevels/sysinit/obsidian-mkmountpoints"
        if not os.path.exists(_dst):
            os.symlink("/etc/init.d/obsidian-mkmountpoints", _dst)
    _write_fstab(f"{mount_dir}/etc/fstab", device, fstype, "a")

    print("Populating shared /etc, /var, and /home partitions...")
    for part_label in ["etc_ab", "var_ab", "home_ab"]:
        fs_dir = part_label.split("_")[0]
        tmp_mount_dir = f"/mnt/tmp_{fs_dir}"
        run_command(f"mkdir -p {tmp_mount_dir}")
        try:
            run_command(f"mount {lordo(part_label, device)} {tmp_mount_dir}")
            run_command(f"rsync -aK --delete {mount_dir}/{fs_dir}/ {tmp_mount_dir}/")
        finally:
            run_command(f"umount {tmp_mount_dir}", check=False)
            run_command(f"rmdir {tmp_mount_dir}", check=False)

    # The ESP gets only the GRUB EFI stub (placed by grub-install later).
    # grub-install runs with --efi-directory=/efi --boot-directory=/boot
    # (default), so grub.cfg + GRUB modules + kernel/initramfs live on the
    # rootfs. At boot, grubx64.efi resolves the rootfs by UUID and reads
    # everything from there — it never reads kernel files from the ESP.
    # Old code rsynced /boot/ contents to each ESP; removed as dead weight
    # (saved ~100 MiB per ESP and eliminated the "which kernel is real?"
    # confusion on kernel upgrades).

    print("Mounting shared partitions for potential chroot...")
    # NOTE: root_a is already mounted at mount_dir (line above). Mount the
    # other 4 partitions for chroot use; don't use _mount_slot_chroot here
    # because that would try to remount root_a on top of itself.
    for sub in ("efi", "etc", "var", "home"):
        run_command(f"mkdir -p {mount_dir}/{sub}")
    for cmd in [
        f"mount {lordo('ESP_A', device)} {mount_dir}/efi",
        f"mount {lordo('etc_ab', device)} {mount_dir}/run/etc_ab --mkdir",
        f"mount {lordo('var_ab', device)} {mount_dir}/var",
        f"mount {lordo('home_ab', device)} {mount_dir}/home",
    ]:
        run_command(cmd)

    print("Copying support files to slot 'a'...")
    script_path = os.path.realpath(sys.argv[0])
    os_release_path = "/etc/os-release"
    obsidianctl_dest = f"{mount_dir}/usr/bin/obsidianctl"
    if os.path.exists(f"{mount_dir}/obsidianctl-aur-installed"):
        print(
            "obsidianctl has been installed through the AUR. Skipping obsidianctl copy..."
        )
    else:
        run_command(f"mkdir -p {mount_dir}/usr/bin")
        run_command(f"cp {script_path} {obsidianctl_dest}")
        run_command(f"chmod +x {obsidianctl_dest}")
    if os.path.exists(os_release_path):
        run_command(f"cp {os_release_path} {mount_dir}/etc/os-release")
    else:
        print(
            f"Warning: os-release file not found at {os_release_path}. Skipping.",
            file=sys.stderr,
        )

    if os.path.exists("/usr/share/pixmaps/obsidianos.png"):
        run_command(f"mkdir -p {mount_dir}/usr/share/pixmaps/")
        run_command(
            f"cp /usr/share/pixmaps/obsidianos.png {mount_dir}/usr/share/pixmaps/obsidianos.png"
        )
    else:
        print(
            f"Warning: ObsidianOS Logo file wasn't found. Skipping.",
            file=sys.stderr,
        )
    print("\nSlot 'a' is now configured and mounted.")
    chroot_confirm = input(
        "Do you want to chroot into slot 'a' to make changes before copying it to slot B? (y/N): "
    )
    if chroot_confirm.lower() == "y":
        print(f"Entering chroot environment in {mount_dir}...")
        print(
            "Common tasks: passwd, ln -sf /usr/share/zoneinfo/Region/City /etc/localtime, useradd"
        )
        print("Type 'exit' or press Ctrl+D when you are finished.")
        _chroot(mount_dir, check=False)
        print("Exited chroot.")

    if args.secure_boot:
        print("Setting up Secure Boot...")
        _chroot(mount_dir, "sbctl create-keys || true", check=False)
        _chroot(mount_dir, "sbctl sign-all || true", check=False)

    print("Unmounting slot 'a' partitions before copy...")
    run_command(f"umount -R {mount_dir}")
    print("Copying system to slot 'b'...")
    source_mount_point = "/mnt/obsidian_source_a"
    target_mount_point = "/mnt/obsidian_target_b"
    run_command(f"mkdir -p {source_mount_point} {target_mount_point}")
    try:
        run_command(f"mount {part3} {source_mount_point}")
        run_command(f"mount {part4} {target_mount_point}")
        run_command(
            f"rsync -aHAX --inplace --delete --info=progress2 {source_mount_point}/ {target_mount_point}/"
        )
    finally:
        run_command(f"umount {source_mount_point}", check=False)
        run_command(f"umount {target_mount_point}", check=False)
        run_command(f"rm -r {source_mount_point} {target_mount_point}", check=False)
    run_command(f"e2label {part4} root_b")
    print("Correcting fstab for slot 'b'...")
    mount_b_dir = "/mnt/obsidian_install_b"
    def _do_slot_b_fstab():
        _write_fstab(f"{mount_b_dir}/etc/fstab", device, fstype, "b")
    _with_temp_mount(part4, mount_b_dir, _do_slot_b_fstab, cleanup_dir=False)
    run_command(f"rm -r {mount_b_dir}", check=False)

    if not args.use_systemdboot:
        mount_dir = "/mnt/obsidianos-install-grub"
        run_command(f"mkdir -p {mount_dir}")
        print("Installing GRUB to ESP_A...")
        _install_grub_to_slot(mount_dir, device, "a", args.use_grub2)
        print("Installing GRUB to ESP_B...")
        _install_grub_to_slot(mount_dir, device, "b", args.use_grub2)
    else:
        print("Installing systemd-boot to ESP_A...")
        _install_systemdboot_to_esp(part1, "a")
        print("Installing systemd-boot to ESP_B...")
        _install_systemdboot_to_esp(part2, "b")

        root_a_partuuid = run_command(
            f"blkid -s PARTUUID -o value {part3}", capture_output=True, text=True
        ).stdout.strip()
        root_b_partuuid = run_command(
            f"blkid -s PARTUUID -o value {part4}", capture_output=True, text=True
        ).stdout.strip()
        if not root_a_partuuid or not root_b_partuuid:
            print(
                "Could not determine PARTUUIDs for root partitions. Cannot create boot entries.",
                file=sys.stderr,
            )
            sys.exit(1)

        loader_conf = "timeout 0\ndefault obsidian-a.conf\n"
        entry_a_conf = (
            "title ObsidianOS (Slot A)\n"
            "linux /vmlinuz-linux\n"
            "initrd /initramfs-linux.img\n"
            f"options root=PARTUUID={root_a_partuuid} rw\n"
        )
        entry_b_conf = (
            "title ObsidianOS (Slot B)\n"
            "linux /vmlinuz-linux\n"
            "initrd /initramfs-linux.img\n"
            f"options root=PARTUUID={root_b_partuuid} rw\n"
        )

        print("Writing boot configuration to ESP_A...")
        _write_systemdboot_config_to_esp(part1, "a", loader_conf, entry_a_conf, entry_b_conf)
        print("Writing boot configuration to ESP_B...")
        _write_systemdboot_config_to_esp(part2, "b", loader_conf, entry_a_conf, entry_b_conf)
        # Old code unconditionally rm'd {mount_dir} here. mount_dir was the
        # GRUB-install scratch dir which isn't created on the systemd-boot path,
        # so the rm was a no-op or error. Removed.
    print("\nInstallation complete!")
    print("Default boot order will attempt Slot A, then Slot B.")
    print("Reboot your system to apply changes.")
