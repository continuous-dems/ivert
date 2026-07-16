#!/bin/bash
# To start the server, run "server_proc.sh"
# To end/kill the server, add the -k flag: "server_proc.sh -k"
# To list the process running, add the -l flag: "server_proc.sh -l"

while getopts "klrh" OPTION;
do
  case "$OPTION" in
    k)
      # Kill the server
      echo "pkill -e -f 'python3 server_maintain_manager.py'"
      pkill -e -f "python3 server_maintain_manager.py"
      echo "pkill -e -f 'python3 server_job_manager.py'"
      pkill -e -f "python3 server_job_manager.py"
      exit 0
      ;;
    l)
      # List the running server processes
      pgrep -a python3 | grep -e server_maintain_manager\.py -e server_job_manager\.py
      exit 0
      ;;
    r)
      # Restart the server
      echo "pkill -e -f 'python3 server_maintain_manager.py'"
      pkill -e -f "python3 server_maintain_manager.py"
      echo "pkill -e -f 'python3 server_job_manager.py'"
      pkill -e -f "python3 server_job_manager.py"
      # Echo the command back to the user.
      echo "nohup python3 server_maintain_manager.py -v >> /mnt/uvol0/ivert_data/ivert_server.log 2>&1 <&- &"
      # Start the server
      nohup python3 server_maintain_manager.py -v >> /mnt/uvol0/ivert_data/ivert_server.log 2>&1 <&- &
      exit 0
      ;;
    h)
      # Print a help message.
      echo "Usage: server_proc.sh [-klrh]"
      echo "  (no option): Start the server"
      echo "  -k: Kill the server"
      echo "  -l: List the running server processes"
      echo "  -r: Restart the server (kill existing, then start)"
      echo "  -h: Print this help message"
      exit 0
      ;;
    *)
      echo "Unknown option"
      exit 1
      ;;
  esac
done

# By default, start the server.
# Echo the command back to the user.
echo "nohup python3 server_maintain_manager.py -v >> /mnt/uvol0/ivert_data/ivert_server.log 2>&1 <&- &"
# Start the server
nohup python3 server_maintain_manager.py -v >> /mnt/uvol0/ivert_data/ivert_server.log 2>&1 <&- &
