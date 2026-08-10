import ast
import configparser
import datetime
import importlib.resources
import os
import re
import sys

from ivert.utils import is_aws

ivert_default_configfile = str(
    importlib.resources.files("ivert").joinpath("config", "ivert_defaults.ini"),
)

ivert_config = None

# Keys whose values are intentionally kept as relative paths rather than being
# resolved to an absolute path against the configfile's location. These are
# resolved later against a different base directory (e.g. the DEM being
# validated), so both the attribute and "ivert options list" must show the
# original relative string.
_RELATIVE_PATH_KEYS = frozenset({"ivert_results_subdir"})

# Marker written above any option that IVERT automatically comments out in the
# user's config file because the option is no longer recognized.
_AUTO_COMMENT_MARKER = "Automatically commented out by IVERT"


def parse_option_descriptions(configfile: str = ivert_default_configfile):
    """Extract the descriptive comments for each option in a config .ini file.

    configparser discards comments, so the human-readable descriptions written
    above each setting in ivert_defaults.ini are parsed here directly from the
    file text. A description is the block of contiguous "#" comment lines that
    immediately precedes a "key = value" assignment (with no blank line in
    between). Header/section comments separated from any key by a blank line are
    not attached to a setting.

    Returns a dict mapping lower-cased option name -> description string (the
    comment block with leading "# " markers stripped, newlines preserved).
    Options with no preceding comment are omitted.
    """
    descriptions = {}
    comment_buffer = []

    with open(configfile, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line:
                # Blank line ends a comment block so headers don't leak onto keys.
                comment_buffer = []
                continue

            if line.startswith("#"):
                # Drop the leading "#" marker(s) and a single following space,
                # but keep any further indentation so aligned multi-line
                # descriptions (e.g. the verbosity option list) stay aligned.
                content = line.lstrip("#")
                content = content.removeprefix(" ")
                comment_buffer.append(content.rstrip())
                continue

            if line.startswith("[") and line.endswith("]"):
                # Section header (e.g. [DEFAULT], [AWS]).
                comment_buffer = []
                continue

            # An assignment line: attach any accumulated comment block to the key.
            if "=" in line:
                key = line.split("=", 1)[0].strip().lower()
                if key and comment_buffer:
                    descriptions[key] = "\n".join(comment_buffer)
            comment_buffer = []

    return descriptions


def _is_absolute_path(value):
    """Return True if 'value' is an absolute filesystem path on any platform.

    Recognizes Unix roots ("/..."), Windows drive-letter roots ("C:\\..." or
    "C:/..."), and UNC roots ("\\\\server\\share"), independent of the OS this is
    running on, so config files can be resolved consistently across platforms.
    """
    stripped = value.strip()
    # Unix absolute path, or a UNC path written with forward slashes ("//server").
    if stripped.startswith("/"):
        return True
    # Windows UNC path ("\\server\share").
    if stripped.startswith("\\\\"):
        return True
    # Windows drive-letter absolute path ("C:\..." or "C:/...").
    return bool(re.match(r"[A-Za-z]:[\\/]", stripped))


def comment_out_options(configfile: str, keys) -> list[str]:
    """Comment out the given option assignments in an .ini file, in place.

    Each commented-out assignment gets a dated marker line above it explaining
    that IVERT did this automatically, so the user can see what happened (and
    restore the line themselves if they want). Everything else in the file --
    comments, blank lines, section headers, ordering -- is left untouched.

    Multi-line (indented continuation) values are commented out along with the
    assignment line that starts them.

    Returns the list of keys that were actually found and commented out.
    """
    keys = {str(k).strip().lower() for k in keys}
    if not keys:
        return []

    with open(configfile, encoding="utf-8") as f:
        lines = f.readlines()

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    commented_out = []
    new_lines = []
    # True while walking the indented continuation lines of an assignment that
    # was just commented out (those belong to the same option's value).
    in_commented_value = False

    for line in lines:
        stripped = line.strip()

        if in_commented_value:
            if stripped and line[:1] in (" ", "\t") and not stripped.startswith("#"):
                new_lines.append("#" + line)
                continue
            in_commented_value = False

        # Only bare "key = value" / "key : value" assignment lines are candidates.
        if (
            stripped
            and not stripped.startswith(("#", ";", "["))
            and re.search(r"[=:]", stripped)
        ):
            key = re.split(r"[=:]", stripped, maxsplit=1)[0].strip().lower()
            if key in keys:
                new_lines.append(
                    f"# {_AUTO_COMMENT_MARKER} on {today}:"
                    " unrecognized setting name.\n",
                )
                new_lines.append("#" + line)
                commented_out.append(key)
                in_commented_value = True
                continue

        new_lines.append(line)

    if commented_out:
        with open(configfile, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    return commented_out


class Config:
    """A subclass implementation of configparser.ConfigParser(), expect that Config attributes are referenced as object
    attributes rather than in a dictionary.

    So if the .ini file contains the attribute:
         varname = 0
    it is referenced by:
         >> c = configfile.Config()
         >> c.varname
         0

    When initialized, it will check whether it is running in an AWS (Amazon Web Services) cloud environment
    and if so, use the [AWS] section of the configfile.

    All paths are considered relative to the location of the configfile. Absolute paths will be left unchanged.
    All other paths that contain a file-delimeter character ("/" on linux, "\" on Windows) will be joined with the
    path of the configfile and converted to an absolute path.
    The EXCEPTION ot the above rule is if the variable name begins with "s3_", in which case it is assumed to be
    an AWS S3 bucket prefix and will not be converted to an absolute path on the local machine.

    The two sections in the .ini configfile should be [DEFAULT] and [AWS].
    No other sections are read by this object, for now.
    """

    def __init__(
        self,
        configfile: str = ivert_default_configfile,
        ignore_errors: bool = False,
    ):
        """Initializes a new instance of the Config class."""
        self._configfile = os.path.abspath(os.path.realpath(configfile))
        self._config = configparser.ConfigParser()
        self.is_aws = is_aws.is_aws()
        self._user_set_keys = set()

        if not os.path.exists(configfile):
            raise FileNotFoundError(f"Configfile {configfile} not found.")

        self._config.read(configfile)

        # Turn the values of the Config file into attributes.
        # This does not handle sections separately. Change this functionality
        # if I need to use different sections separately.
        self._parse_config_into_attrs()

        # # If we're importing the primary IVERT Config file, add the user variables and S3 creds to the Config as well.
        # if os.path.basename(self._configfile) == os.path.basename(ivert_default_configfile):
        #     self._add_user_variables_and_s3_creds_to_config_obj(ignore_errors=ignore_errors)

        # If loading the defaults config, overlay any user-local overrides on top.
        if os.path.basename(self._configfile) == os.path.basename(
            ivert_default_configfile,
        ):
            self._apply_user_config()

        # If we've generated the Config object for the most-commonly-used IVERT Config file, make it globally available.
        if self._configfile == ivert_default_configfile:
            global ivert_config
            ivert_config = self

    def _abspath(self, path, only_if_actual_path_doesnt_exist=False):
        """Retreive the absolute path of a file path contained in the configfile.

        In this project, absolute paths are relative to the location of the
        configfile. In this case. join them with the path to the Config file and
        return an absolute path rather than a relative path.
        """
        # If we've specified to do this only if the path doesn't exist in its current location,
        # and the path does exist in its current location (either the filename, or the parent directory),
        # then just return the path as-is
        if only_if_actual_path_doesnt_exist and (
            os.path.exists(path) or os.path.exists(os.path.split(path)[0])
        ):
            return path

        return os.path.abspath(os.path.join(os.path.dirname(self._configfile), path))

    def _apply_user_config(self):
        """If the user config file exists, overlay its values on top of the defaults.

        Resolution order for the user config path:
          1. IVERT_USER_CONFIG environment variable (set directly or via --config CLI flag)
          2. user_configfile value from ivert_defaults.ini
        """
        env_override = os.environ.get("IVERT_USER_CONFIG", "").strip()
        if env_override:
            user_path = os.path.abspath(os.path.expanduser(env_override))
        elif hasattr(self, "user_configfile") and self.user_configfile:
            user_path = os.path.abspath(os.path.expanduser(str(self.user_configfile)))
        else:
            return
        if not os.path.exists(user_path):
            return

        user_config = configparser.ConfigParser()
        try:
            user_config.read(user_path)
        except configparser.Error as e:
            print(
                f"WARNING: Could not parse the IVERT user config file {user_path}:"
                f"\n  {e}"
                "\n  Ignoring it and using IVERT's default settings. Fix or delete"
                " that file, or run 'ivert options reset' to start over.",
                file=sys.stderr,
            )
            return

        # Options that no longer exist (or never existed) in ivert_defaults.ini --
        # typically hold-overs from an older IVERT version, or typos. Comment them
        # out of the user's file rather than trying (and failing) to apply them.
        unknown_keys = self._unknown_user_keys(user_config)
        if unknown_keys:
            self._handle_unknown_user_keys(user_path, unknown_keys)

        sections = ["DEFAULT"]
        if self.is_aws and "AWS" in user_config:
            sections.append("AWS")

        saved_configfile = self._configfile
        self._configfile = user_path
        try:
            for section in sections:
                for k in user_config[section]:
                    if k in unknown_keys:
                        continue
                    try:
                        v = user_config[section][k]
                    except configparser.Error as e:
                        # e.g. a hand-edited "%(...)s" reference that can't be
                        # resolved. Skip that option instead of crashing.
                        print(
                            f"WARNING: Ignoring setting '{k}' in {user_path}:\n  {e}",
                            file=sys.stderr,
                        )
                        continue
                    self._read_option(k, v)
                    self._user_set_keys.add(k)
        finally:
            self._configfile = saved_configfile

    def _unknown_user_keys(self, user_config: configparser.ConfigParser) -> set[str]:
        """Return the option names in a user config that IVERT no longer recognizes.

        An option is recognized if it appears anywhere in ivert_defaults.ini (in
        either the [DEFAULT] or the [AWS] section), regardless of which section
        the user put it in.
        """
        known = set(self._config["DEFAULT"].keys())
        if self._config.has_section("AWS"):
            known.update(self._config["AWS"].keys())

        # configparser propagates [DEFAULT] options into every other section, so
        # only the section's own options are checked on top of the defaults.
        user_keys = set(user_config.defaults().keys())
        for section in user_config.sections():
            user_keys.update(user_config[section].keys())

        return user_keys - known

    def _handle_unknown_user_keys(self, user_path: str, unknown_keys: set[str]) -> None:
        """Warn about unrecognized user-config options and comment them out."""
        from ivert import __version__

        key_list = "\n".join(f"    - {k}" for k in sorted(unknown_keys))
        try:
            commented = comment_out_options(user_path, unknown_keys)
        except OSError as e:
            print(
                f"WARNING: The IVERT user config file {user_path} contains settings"
                f" that IVERT v{__version__} does not recognize:"
                f"\n{key_list}"
                f"\n  They will be ignored. IVERT tried to comment them out of that"
                f" file but could not write to it:\n    {e}",
                file=sys.stderr,
            )
            return

        if commented:
            print(
                f"WARNING: The IVERT user config file {user_path} contained settings"
                f" that IVERT v{__version__} does not recognize:"
                f"\n{key_list}"
                "\n  They may have been renamed or removed in a newer version of"
                " IVERT, or misspelled when entered by hand."
                "\n  They have been commented out of that file (with a dated note)"
                " and ignored."
                "\n  Run 'ivert options list' to see the settings IVERT supports.",
                file=sys.stderr,
            )

    def _parse_config_into_attrs(self):
        """Read all the Config lines, put into object attributes. If we're running in an AWS instance, also read the
        [AWS] section.
        """
        # First input the default values from the Config file.
        for k, v in self._config["DEFAULT"].items():
            self._read_option(k, v)

        # Then, if we're running in an AWS environment, read all the values from the [AWS] section (if it exists).
        if self.is_aws and ("AWS" in self._config):
            section = self._config["AWS"]
            for k, v in section.items():
                self._read_option(k, v)

    def _read_option(self, key, value):
        """Read an individual option.

        Will use "ast.literal_eval()"  to parse it,
        and then attempt to read as a boolean if that fails. It helps to keep the
        .ini file a python-readable format, and allows base python objects to be in there.
        """
        try:
            # Using ast.literal_eval() rather than eval(), because literal_eval only allows the creation of generic
            # python objects but doesn't allow the calling of functions or commands that could pose security risks.
            # It will natively evaluate things like lists, dictionaries, or other generic python data types.
            setattr(self, key, ast.literal_eval(value))
            return
        except (NameError, ValueError, SyntaxError):
            pass

        # In some boolean cases, you can put other things besides "True/False", such as "yes/no"
        # Use configparser's boolean vocabulary to try to interpret it as a boolean.
        # The value is converted directly (rather than looked up by key) so that
        # this also works for options coming from the [AWS] section or from the
        # user config file, which need not exist in this parser's [DEFAULT].
        if isinstance(value, str):
            bool_value = configparser.ConfigParser.BOOLEAN_STATES.get(
                value.strip().lower(),
            )
            if bool_value is not None:
                setattr(self, key, bool_value)
                return

        # Check to see if this is potentially a path. Interpret it as such if it is a string and contains path
        # characters ('\' in Windows or '/' in Linux).
        # If this is the case, return the absolute path of that file/directory *relative* to the current directory the
        # Config.ini file is contained.
        try:
            if key.lower() in _RELATIVE_PATH_KEYS:
                # This path is resolved later against a different base directory
                # (not the configfile's location), so keep it relative as-is.
                pass

            elif key[:3].lower() == "s3_":
                # This is an S3 key-path. Do not convert it to an absolute path.
                pass

            elif re.match(r"[a-zA-Z][a-zA-Z0-9+\-.]*://", value.strip()):
                # This is a URL (http://, https://, ftp://, s3://, etc.). Leave it as-is.
                pass

            # Treat the value as a path if it references the home directory ('~') or
            # contains a path separator. Both '/' and '\' are recognized regardless of
            # platform: config files are written with forward slashes (e.g. "~/.ivert"),
            # which are valid on Windows too, so path handling must not depend on
            # sys.platform. os.path.expanduser resolves '~' cross-platform.
            elif ("~" in value) or ("/" in value) or ("\\" in value):
                # If it references the home directory, expand it on the local machine.
                if "~" in value:
                    setattr(self, key, os.path.abspath(os.path.expanduser(value)))
                # If it's already an absolute path, just use it as-is. Recognize both
                # Unix ("/...") and Windows ("C:\...", "C:/...", or UNC "\\...") roots
                # so shared config files resolve correctly on either platform.
                elif _is_absolute_path(value):
                    setattr(self, key, os.path.abspath(value))
                # If it's a relative path, make it relative to the _configfile's directory.
                else:
                    setattr(
                        self,
                        key,
                        self._abspath(
                            os.path.join(os.path.dirname(self._configfile), value),
                        ),
                    )
                return
        except ValueError:
            pass

        # Otherwise, it's probably just a regular string value, set it as-is.
        setattr(self, key, value)
        return

    # def _fill_bucket_names_from_ivert_setup(self, include_sns_arn=True):
    #     """Fills in the bucket name entries in the Config object.
    #
    #     If we're server-side, we need to fill in [s3_bucket_database], [s3_bucket_trusted], and [s3_bucket_export],
    #     and [s3_bucket_quarantine].
    #     These can be found in the ivert_setup/setup/paths.sh file from the ivert_setup repository."""
    #     try:
    #         assert hasattr(self, "s3_bucket_database")
    #         assert hasattr(self, "s3_bucket_import_trusted")
    #         assert hasattr(self, "s3_bucket_export_server")
    #         assert hasattr(self, "s3_bucket_quarantine")
    #     except AssertionError:
    #         print("Error: Not all required bucket names are present in the ivert_setup 'paths.sh' file.",
    #               file=sys.stderr)
    #         sys.exit(0)
    #
    #     if include_sns_arn:
    #         assert hasattr(self, "sns_topic_arn")
    #
    #     if not os.path.exists(self.ivert_setup_paths_file):
    #         raise FileNotFoundError(f"ivert_setup_paths_file not found: {self.ivert_setup_paths_file}")
    #
    #     with open(self.ivert_setup_paths_file, 'r') as f:
    #         paths_text_lines = [line.strip() for line in f.readlines()]
    #
    #     # Get the S3 bucket names from the paths.sh file
    #     # For each variable, look for the line that starts with it, extract the value after the =,
    #     # and strip off any comments.
    #
    #     # Read the database bucket from paths.sh
    #     try:
    #         db_line = [line for line in paths_text_lines
    #                    if re.match(r"^s3_bucket_database(?!\w)", line.lstrip().lower())][0]
    #         self.s3_bucket_database = db_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_database == '':
    #             self.s3_bucket_database = None
    #     except IndexError:
    #         self.s3_bucket_database = None
    #
    #     # Read the import bucket from paths.sh
    #     try:
    #         trusted_line = [line for line in paths_text_lines
    #                         if re.match(r"^s3_bucket_import_trusted(?!\w)", line.lstrip().lower())][0]
    #         self.s3_bucket_import_trusted = trusted_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_import_trusted == '':
    #             self.s3_bucket_import_trusted = None
    #     except IndexError:
    #         self.s3_bucket_import_trusted = None
    #
    #     # Read the untrusted bucket from paths.sh (if it exists there.
    #     # It usually shouldn't, but it'll read it if it's there.)
    #     try:
    #         untrusted_line = [line for line in paths_text_lines
    #                           if re.match(r"^s3_bucket_import_untrusted(?!\w)", line.lstrip().lower())][0]
    #         self.s3_bucket_import_untrusted = untrusted_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_import_untrusted == '':
    #             self.s3_bucket_import_untrusted = None
    #     except IndexError:
    #         self.s3_bucket_import_untrusted = None
    #
    #     # Read the export_server bucket from paths.sh
    #     try:
    #         search_str = r"^s3_bucket_export_server(?!\w)"
    #
    #         export_server_line = [line for line in paths_text_lines
    #                               if re.match(search_str, line.lstrip().lower())][0]
    #         self.s3_bucket_export_server = export_server_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_export_server == '':
    #             self.s3_bucket_export_server = None
    #     except IndexError:
    #         self.s3_bucket_export_server = None
    #
    #     # Read the export_alt bucket from paths.sh
    #     try:
    #         search_str = r"^s3_bucket_export_alt(?!\w)"
    #
    #         export_alt_line = [line for line in paths_text_lines
    #                            if re.match(search_str, line.lstrip().lower())][0]
    #         self.s3_bucket_export_alt = export_alt_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_export_alt == '':
    #             self.s3_bucket_export_alt = None
    #     except IndexError:
    #         self.s3_bucket_export_alt = None
    #
    #     # Read the export_client bucket from paths.sh. Should be empty or not there at all.
    #     try:
    #         if self.is_aws and self.use_export_alt_bucket:
    #             search_str = r"^s3_bucket_export_alt(?!\w)"
    #
    #             # Also update the export_server prefix if we're using the alternate bucket
    #             self.s3_ivert_jobs_database_client_key = self.s3_ivert_jobs_database_alt_client_key
    #         else:
    #             search_str = r"^s3_bucket_export_client(?!\w)"
    #
    #         export_client_line = [line for line in paths_text_lines
    #                               if re.match(search_str, line.lstrip().lower())][0]
    #         self.s3_bucket_export_client = export_client_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         if self.s3_bucket_export_client == '':
    #             self.s3_bucket_export_client = None
    #     except IndexError:
    #         self.s3_bucket_export_client = None
    #
    #     # Read the quarantine bucket from paths.sh
    #     try:
    #         quarantine_line = [line for line in paths_text_lines
    #                            if re.match(r"^s3_bucket_quarantine(?!\w)", line.lstrip().lower())][0]
    #         self.s3_bucket_quarantine = quarantine_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #     except IndexError:
    #         self.s3_bucket_quarantine = None
    #
    #     if include_sns_arn:
    #         try:
    #             sns_line = [line for line in paths_text_lines
    #                         if re.match(r"^cudem_sns_arn(?!\w)", line.lstrip().lower())][0]
    #             self.sns_topic_arn = sns_line.split("=")[1].split("#")[0].strip().strip("'").strip('"')
    #         except IndexError:
    #             self.sns_topic_arn = None
    #
    #     # Check to see if any of these just reference other variables. If so, fill them in. This could just point
    #     # to another variable, so keep looping until we've gotten an actual value.
    #     for varname in ["s3_bucket_database",
    #                     "s3_bucket_import_untrusted",
    #                     "s3_bucket_import_trusted",
    #                     "s3_bucket_export_server",
    #                     "s3_bucket_export_alt",
    #                     "s3_bucket_export_client",
    #                     "s3_bucket_quarantine"]:
    #         if getattr(self, varname) is None:
    #             continue
    #
    #         # Since we're reading from a bash shell script, variables are defined as $varname.
    #         while getattr(self, varname).find("$") > -1:
    #             varname_from = getattr(self, varname).replace("$", "")
    #             setattr(self, varname, getattr(self, varname_from))
    #
    #     return

    # def _add_user_variables_and_s3_creds_to_config_obj(self, ignore_errors: bool = False):
    #     """Add the names of the S3 buckets to the configfile.Config object.
    #
    #     On a client instance, src setup needs to be run to flesh out the user configfile, before this will work."""
    #     # Make sure all these are defined in here. They may be assigned to None but they should exist. This is
    #     # a sanity check in case we changed the bucket variables names in the configfile.
    #     try:
    #         assert hasattr(self, "s3_bucket_import_untrusted")
    #         assert hasattr(self, "s3_bucket_export_client")
    #         assert hasattr(self, "s3_export_client_endpoint_url")
    #         assert hasattr(self, "s3_import_untrusted_endpoint_url")
    #         assert hasattr(self, "user_email")
    #         assert hasattr(self, "username")
    #         assert hasattr(self, "aws_profile_ivert_import_untrusted")
    #         assert hasattr(self, "aws_profile_ivert_export_client")
    #         assert hasattr(self, "aws_profile_ivert_export_alt")
    #         assert hasattr(self, "use_export_alt_bucket")
    #     except AssertionError as e:
    #         if ignore_errors:
    #             pass
    #         else:
    #             raise e

    # If we're on the server side (in the AWS), get these from the "ivert_setup" repository under /setup/paths.sh.
    #    In this case, only the s3_bucket_import_trusted, s3_bucket_database, and s3_bucket_export are needed.
    # if self.is_aws:
    #     self._fill_bucket_names_from_ivert_setup()

    # If we're on the client side (not in an AWS instance), get these from the user configfile.
    # else:
    #     try:
    #         if os.path.exists(self.user_configfile):
    #             user_config = Config(self.user_configfile)
    #             self.user_email = user_config.user_email
    #             self.username = user_config.username
    #             self.aws_profile_ivert_import_untrusted = user_config.aws_profile_ivert_import_untrusted
    #             self.aws_profile_ivert_export_client = user_config.aws_profile_ivert_export_client
    #             self.aws_profile_ivert_export_alt = user_config.aws_profile_ivert_export_alt
    #
    #         # Now try to read the s3 credentials file.
    #         if os.path.exists(os.path.abspath(self.ivert_s3_credentials_file)):
    #             s3_credentials = Config(self.ivert_s3_credentials_file)
    #             self.s3_bucket_import_untrusted = s3_credentials.s3_bucket_import_untrusted
    #             self.s3_import_untrusted_endpoint_url = s3_credentials.s3_import_untrusted_endpoint_url
    #
    #             self.s3_bucket_export_client = s3_credentials.s3_bucket_export_client
    #             self.s3_export_client_endpoint_url = s3_credentials.s3_export_client_endpoint_url
    #
    #             self.s3_bucket_export_alt = s3_credentials.s3_bucket_export_alt
    #             self.s3_export_alt_endpoint_url = s3_credentials.s3_export_alt_endpoint_url
    #
    #
    #     except AttributeError as e:
    #         if ignore_errors:
    #             pass
    #         else:
    #             raise e

    # return
