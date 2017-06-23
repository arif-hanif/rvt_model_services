""" process_model.py
Usage:
    process_model.py    [options] 
                        <command> <project_code> 
                        <revit_model_path> <revit_model_file_name> 
                        <revit_version_path> <revit_version> <timeout>

Arguments:
    command             action to be run on model, like: qc or dwf
                        currently available: qc, dwf
    project_code        unique project code consisting of 'projectnumber_projectModelPart' 
                        like 456_11 , 416_T99 or 377_S
    model_path          revit model path without file name
    model_file_name     revit model file name
    rvt_version_path    revit .exe path of appropriate version like: 
                        "C:/Program Files/Autodesk/Revit Architecture 2015/Revit.exe"
                        soon depracted: replaced by autodetection
    rvt_version         the revit main version number like: 2015
                        soon depracted: replaced by autodetection
    timeout             timeout in seconds before revit process gets terminated

Options:
    -h, --help          Show this help screen.
    --html_path=<html>  path to store html bokeh graphs, default in /commands/qc/*.html
"""

from docopt import docopt
import os.path as op
import os
import re
import winreg
import subprocess
import importlib
import psutil
import time
import logging
import colorful
import olefile
import rps_xml
import rvt_journal_writer
from collections import defaultdict
from commands.qc.bokeh_qc_graphs import update_graphs
from commands.warnings.bokeh_warnings_graphs import update_json_and_bokeh

# TODO write model not found to log -> to main log from logging
# TODO write log header if log not exists with logging module?
# TODO make rvt_pulse available from process model?
# TODO audit parse journal files post-process to discover potential model corruption


def get_paths_dict():
    """
    Maps path structure into a dict.
    :return:dict: path lookup dictionary
    """
    path_dict = defaultdict()

    current_dir = op.dirname(op.abspath(__file__))
    root_dir = current_dir
    journals_dir = op.join(root_dir, "journals")
    logs_dir = op.join(root_dir, "logs")
    warnings_dir = op.join(root_dir, "warnings" + op.sep)
    commands_dir = op.join(root_dir, "commands")
    com_warnings_dir = op.join(commands_dir, "warnings")
    com_qc_dir = op.join(commands_dir, "qc")

    path_dict["root_dir"] = root_dir
    path_dict["logs_dir"] = logs_dir
    path_dict["warnings_dir"] = warnings_dir
    path_dict["journals_dir"] = journals_dir
    path_dict["commands_dir"] = commands_dir
    path_dict["com_warnings_dir"] = com_warnings_dir
    path_dict["com_qc_dir"] = com_qc_dir

    for pathname in path_dict.keys():
        print(" {} - {}".format(pathname, path_dict[pathname]))

    return path_dict


def rvt_journal_run(program, journal_file, cwd):
    """
    Starts an instance of rvt processing the instructions of the journal file.
    :param program: executable to start
    :param journal_file: journal file path as command argument
    :return:
    """
    return psutil.Popen([program, journal_file], cwd=cwd,
                        shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def get_rvt_file_version(rvt_file):
    """
    Seraches for the BasiFileInfo stream in the rvt file ole structure.
    :param rvt_file: model file path
    :return:str: rvt_file_version
    """
    if olefile.isOleFile(rvt_file):
        rvt_ole = olefile.OleFileIO(rvt_file)
        file_info = rvt_ole.openstream("BasicFileInfo").read().decode("utf-16le", "ignore")
        pattern = re.compile(r" \d{4} ")
        rvt_file_version = re.search(pattern, file_info)[0].strip()
        return rvt_file_version
    else:
        print(f"file does not appear to be an ole file: {rvt_file}")


def installed_rvt_detection(search_version):
    """
    Finds install path of rvt versions in win registry
    :param search_version: major version number
    :return:str: install path
    """
    search_version = str(search_version)
    reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
    soft_uninstall = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
    install_keys = winreg.OpenKey(reg, soft_uninstall)

    install_location = "InstallLocation"
    rvt_reg_keys = {}
    rvt_install_paths = {}

    index = 0
    while True:
        try:
            adsk_pattern = r"Autodesk Revit ?(\S* )?\d{4}$"
            current_key = winreg.EnumKey(install_keys, index)
            if re.match(adsk_pattern, current_key):
                rvt_reg_keys[current_key] = index
                # print([current_key, index])
        except OSError:
            break
        index += 1

    for rk in rvt_reg_keys.keys():
        version_pattern = r"\d{4}"
        rvt_install_version = re.search(version_pattern, rk)[0]
        reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
        rvt_reg = winreg.OpenKey(reg, soft_uninstall + "\\" + rk)
        # print([rk, rvt_reg, install_location])
        exe_location = winreg.QueryValueEx(rvt_reg, install_location)[0] + "Revit.exe"
        rvt_install_paths[rvt_install_version] = exe_location

    return rvt_install_paths[search_version]


def command_detection(search_command, commands_dir, rvt_ver, root_dir, project_code):
    """
    Searches command paths for register dict in __init__.py in command roots to
    prepare appropriate command strings to be inserted into the journal file
    :param search_command: command name to look up
    :param commands_dir: commands directory
    :param rvt_ver: rvt version
    :param root_dir:
    :param project_code:
    :return:
    """
    com_dict = defaultdict()
    found_dir = False
    for directory in os.scandir(commands_dir):
        command_name = directory.name
        # print(command_name)
        if search_command == command_name:
            found_dir = True
            # print(f" found appropriate command directory {op.join(commands_dir, command_name)}")
            if op.exists(f"{commands_dir}/{command_name}/__init__.py"):
                mod = importlib.machinery.SourceFileLoader(command_name, op.join(commands_dir,
                                                                                 command_name,
                                                                                 "__init__.py")).load_module()
            else:
                print(colorful.bold_red(f" appropriate __init__.py in command directory not found - aborting."))
                exit()
            if "register" in dir(mod):
                if mod.register["name"] == command_name:
                    # print("command_name found!")
                    if "get_rps_button" in mod.register:
                        # print("needs rps button")
                        button_name = mod.register["get_rps_button"]
                        rps_button = rps_xml.get_rps_button(rps_xml.find_xml_command(rvt_ver, ""), button_name)
                        com_dict[command_name] = rps_button
                    if "rvt_journal_writer" in mod.register:
                        # print("needs rvt_journal_writer")
                        if mod.register["rvt_journal_writer"] == "warnings_export_command":
                            warnings_command_dir = op.join(root_dir, "warnings" + op.sep)
                            warn_cmd = rvt_journal_writer.warnings_export_command(rvt_journal_writer.
                                                                                  export_warnings_template,
                                                                                  warnings_command_dir,
                                                                                  project_code,
                                                                                  ),
                            com_dict[command_name] = warn_cmd[0]
                        elif mod.register["rvt_journal_writer"] == "audit":
                            com_dict[command_name] = "' "
    if not found_dir:
        print(colorful.bold_red(f" appropriate command directory for '{search_command}' not found - aborting."))
        exit()
    return com_dict


args = docopt(__doc__)

command = args["<command>"]
project_code = args["<project_code>"]
model_path = args["<revit_model_path>"]
model_file_name = args["<revit_model_file_name>"]
rvt_version_path = args["<revit_version_path>"]
rvt_version = args["<revit_version>"]
timeout = int(args["<timeout>"])
html_path = args["--html_path"]

print(colorful.bold_blue(f"+process model job control started with command: {command}"))
print(colorful.bold_orange('-detected following path structure:'))
paths = get_paths_dict()

semicolon_concat_args = ";".join([f"{k}={v}" for k, v in args.items()])
comma_concat_args = ",".join([f"{k}={v}" for k, v in args.items()])

rvt_model_path = model_path + model_file_name
journal_file_path = op.join(paths["journals_dir"], project_code + ".txt")
model_exists = op.exists(rvt_model_path)

if not html_path:
    if command == "qc":
        html_path = paths["com_qc_dir"]
    elif command == "warnings":
        html_path = paths["com_warnings_dir"]
elif not os.path.exists(html_path):
    if command == "qc":
        html_path = paths["com_qc_dir"]
        print(f"your specified html path was not found - will export html graph to {paths['com_qc_dir']} instead")
    elif command == "warnings":
        html_path = paths["com_warnings_dir"]
        print(f"your specified html path was not found - will export html graph to {paths['com_warnings_dir']} instead")

job_logging = op.join(paths["logs_dir"], "job_logging.csv")
header_logging = "time_stamp;level;project;process_hash;error_code;args\n"
if not op.exists(job_logging):
    with open(job_logging, "w") as logging_file:
        logging_file.write(header_logging)
    print(colorful.bold_blue(f"logging goes to: {job_logging}"))

logging.basicConfig(format='%(asctime)s;%(levelname)s;%(message)s',
                    datefmt="%Y%m%dT%H%M%SZ",
                    filename=job_logging,
                    level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("bokeh").setLevel(logging.CRITICAL)

print(colorful.bold_orange('-detected following process structure:'))
current_proc_hash = hash(psutil.Process())
print(f" current process hash: {colorful.cyan(current_proc_hash)}")
logging.info(f"{project_code};{current_proc_hash};;{comma_concat_args};{'task_started'}")

os.environ["RVT_QC_PRJ"] = project_code
os.environ["RVT_QC_PATH"] = rvt_model_path
os.environ["RVT_LOG_PATH"] = paths["logs_dir"]

cmd_dict = command_detection(command, paths["commands_dir"], rvt_version, paths["root_dir"], project_code)
# print(cmd_dict)

if model_exists:

    if command == "audit":
        journal_template = rvt_journal_writer.audit_detach_template
    else:
        journal_template = rvt_journal_writer.detach_rps_template

    journal = rvt_journal_writer.write_journal(journal_file_path,
                                               journal_template,
                                               model_path,
                                               model_file_name,
                                               cmd_dict[command],
                                               )

    addin_file_path = op.join(paths["journals_dir"], "RevitPythonShell.addin")
    rps_addin = rvt_journal_writer.write_addin(addin_file_path,
                                               rvt_journal_writer.rps_addin_template,
                                               rvt_version,
                                               )

    run_proc = rvt_journal_run(rvt_version_path, journal_file_path, paths["root_dir"])
    run_proc_id = run_proc.pid
    run_proc_name = run_proc.name()

    print(f" initiating process id: {run_proc_id} - {run_proc_name}")

    # let's wait a second for rvt process to fire up
    time.sleep(1)

    child_proc = run_proc.children()[0]
    child_pid = run_proc.children()[0].pid
    if child_proc.name() == "Revit.exe":
        proc_name_colored = colorful.bold_green(child_proc.name())
    else:
        proc_name_colored = colorful.bold_red(child_proc.name())

    print(f" number of child processes: {len(run_proc.children())}")
    print(f" first child process: {child_pid} - {proc_name_colored}")

    rvt_model_version = get_rvt_file_version(rvt_model_path)
    print(colorful.bold_orange(f"-detected model revit version: {rvt_model_version}"))

    rvt_install_path = installed_rvt_detection(rvt_model_version)
    print(colorful.bold_orange("-detected installed revit at path:"))
    print(f" {rvt_install_path}")

    print(colorful.bold_orange("-process countdown:"))
    print(f" timeout until termination of process: {child_pid} - {proc_name_colored}:")

    # the main timeout loop
    for sec in range(timeout):
        time.sleep(1)
        print(f" {str(timeout-sec).zfill(4)} seconds", end="\r")
        poll = run_proc.poll()

        if poll == 0:
            print(colorful.bold_green(f" {poll} - revit finished!"))
            logging.info(f"{project_code};{current_proc_hash};0")

            if command == "qc":
                update_graphs(project_code, html_path)
            break

        elif timeout-sec-1 == 0:
            print("\n")
            print(colorful.bold_red(" timeout!!"))
            if not poll:
                print(colorful.bold_red(f" kill child process now: {child_pid}"))
                child_proc.kill()
                if command != "warnings":
                    logging.warning(f"{project_code};{current_proc_hash};1")

    # post loop processing updating graphs, parsing journal files
    if command == "warnings":
        update_json_and_bokeh(project_code, html_path)
        logging.info(f"{project_code};{current_proc_hash};0")
    elif command == "audit":
        pass

else:
    print("model not found")

print(colorful.bold_blue("+process model job control script ended"))

if args["<revit_version_path>"] or args["<revit_version>"]:
    print(colorful.bold_red("!!warning!!: <revit_version_path> and <revit_version> will be \n"
                            "replaced by autodetect functions and depracted in 0.3!"))
