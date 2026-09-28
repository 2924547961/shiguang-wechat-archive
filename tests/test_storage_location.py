from wxdesk.storage_location import archive_choice, public_location, save_archive_choice


def test_new_install_uses_user_data_location_and_can_choose_folder(tmp_path):
    install = tmp_path / 'Desktop' / 'app'
    install.mkdir(parents=True)
    config = tmp_path / 'AppData' / 'Shiguang' / 'archive_location.json'
    suggested, needs_picker = archive_choice(base=install, config=config)
    assert needs_picker
    assert suggested == config.parent / 'Archive'
    assert not suggested.is_relative_to(install)

    chosen = tmp_path / 'Documents' / 'MyArchive'
    chosen.mkdir(parents=True)
    save_archive_choice(chosen, config=config)
    selected, needs_picker = archive_choice(base=install, config=config)
    assert selected == chosen and not needs_picker
    assert public_location(suggested, config=config)['next'] == str(chosen)


def test_existing_project_archive_is_kept_until_user_changes_it(tmp_path):
    install = tmp_path / 'app'
    legacy = install / '.shiguang'
    legacy.mkdir(parents=True)
    config = tmp_path / 'AppData' / 'archive_location.json'
    assert archive_choice(base=install, config=config) == (legacy, False)
    explicit = tmp_path / 'other'
    assert archive_choice(explicit, base=install, config=config) == (explicit, False)
