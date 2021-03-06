import socket
import threading
import os
import logging
import yaml
from pathlib import Path
from inputimeout import inputimeout, TimeoutOccurred
import argparse
import time
import constants
from main_serv import MainServerConn

from p2p import myUDPClient, myUDPServer


import sys
sys.path.append(str(Path(__file__).parent.parent.absolute()))
print(Path(__file__).parent.parent.absolute())

from client_utils import ClientFile
from utils import verify_hash

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class Client():
    def __init__(self, config_file_path):
        # reach out to server to share config
        if not Path(config_file_path).is_file():
            logger.error("Config file does not exist")
            exit(1)
        with open(config_file_path, 'r') as f:
            config = yaml.load(f, Loader=yaml.Loader)
        print("Client with following config is being loaded.. (10s to init)")
        print(config)
        time.sleep(10.0)
        self.client_id = config['CLIENTID']
        self.file_vector = config['FILE_VECTOR']
        self.my_port = config['MYPORT']
        self.serv_port = config['SERVERPORT']
        self.file_loca = Path(config_file_path).parent
        # set downloads path for client
        self.down_loca = (self.file_loca/'downloads')
        self.down_loca.mkdir(parents=True, exist_ok=True)
        self.client_file_mgr = ClientFile(self.file_vector, self.file_loca)
        logging.info("Done with config initialization")

        self.client_shutdown = False

        # send init message to server
        self.serv_conn = MainServerConn(config, self.serv_port)
        print("INIT DONE")
        if (not self.serv_conn.get_conn_status()):
            return
        # then start listening on port
        my_server = myUDPServer(self.my_port, self.client_file_mgr)
        if (not my_server.check_success()):
            print("P2P Server init failed. Closing client")
            self.serv_conn.set_close()
            return
        else:
            server = threading.Thread(target=my_server.listen)
            server.start()

        # open user input
        input_thread = threading.Thread(target=self.user_input)
        input_thread.start()

        while (not self.client_shutdown and self.serv_conn.get_conn_status()):
            # do nothing
            pass
        if (not self.serv_conn.get_conn_status()):
            logger.error("Connection to server lost, client exiting..")
            self.serv_conn.set_close()
            my_server.set_close()

        if (self.client_shutdown):
            logger.info("Client shutdown called, client exiting")
            if (self.serv_conn.get_conn_status()):
                self.serv_conn.set_close()
                my_server.set_close()

        print("Client shutting down...")
        input_thread.join()
        logger.info("input thread joined")
        server.join()
        logger.info("my server joined")
        del self.serv_conn

    def move_window(self, seq_nos):
        '''
        Function to move the client window
        param seq_nos : dictionary with window of current sequence numbers
        '''
        for i in range(len(seq_nos)):
            seq_nos[i] = (seq_nos[i] + 1) % (constants.MAX_SEQ_NO)

    def write_buffer(self, buffer, writer):
        '''
        Function to writer buffer to writer object
        param buffer : list of byte buffers to write to disk
        param writer : writer object of type WriteObj used to write to file
        '''
        for i in range(len(buffer)):
            writer.write(buffer[i])
        buffer.clear()

    def request_cleanup(self, hash, writer, abnormal=False):
        '''
        Function to do cleanup after request is complete
        param hash : hash of the file, if hash is None, consider it as empty
                     file
        param writer : writer object of type WriteObj used to write to file
        param abnormal (False) : flag to signal if abnormal closure of request
        '''
        file_loc = writer.get_filepath()
        del writer
        if (self.serv_conn.get_conn_status()):
            # connection ended abnormally
            if (abnormal):

                # remove file
                if os.path.exists(file_loc):
                    logger.info(f"File deleted: {file_loc} due to server"
                                "connection lost")
                    os.remove(file_loc)
                    return

            # empty file, do nothing
            if (hash is None):
                logger.info(f"Empty file detected: {file_loc}")
                return
            if (verify_hash(hash, file_loc)):
                # send success to server
                # TODO: send success message to server
                logger.info(f"File hash verified: {file_loc}")
                return
            # failed hash check
            else:
                # remove file
                if os.path.exists(file_loc):
                    logger.info(f"File deleted: {file_loc} due to failed"
                                " hash check")
                    os.remove(file_loc)
        # lost connection to server, delete file
        else:
            # remove file
            if os.path.exists(file_loc):
                logger.info(f"File deleted: {file_loc} due to main server"
                            " connection lost")
                os.remove(file_loc)

    def _cleanup_abnormal(self, seq_no, hash, writer, client_sock):
        '''
        Function to handle abnormal cleanup. Send ACK for message run cleanup
        and finally delete socket object
        param seq_no : seq_no of last packet to send ACK for
        param hash : hash of the file requested
        param writer : writer object used to write to disk
        param client_sock: client socket object used to communicate with peer
        '''
        try:
            logger.error("Connection reset or closed by"
                         f" {client_sock.get_addr()}")
            client_sock.send_ack(seq_no, True)
            self.request_cleanup(hash, writer, True)
        finally:
            del client_sock

    def request_file(self, filename, addr, writer):
        '''
        Function to handle request file from a peer
        param filename : name/number of the file to request from peer
        param addr : address to find peer at
        param writer : writer object used to writer to disk
        '''
        client_sock = myUDPClient(addr)
        seq_no = (0).to_bytes(1, 'big')
        pkt_type = constants.DATA_PACKET.to_bytes(1, "big")
        filename = int(filename).to_bytes(1, "big")
        data = seq_no + pkt_type + filename
        client_sock.send(data)
        client_buffer = []
        retries = constants.CLIENT_MAX_RETRIES
        # starting sequence numbers that it should get from the server
        seq_nos = [0, 1]
        connEnd = False
        second_end = False  # if buffer has the ending packet
        hash = None  # hash of the incoming file

        while retries >= 0 and self.serv_conn.get_conn_status() \
                and (not connEnd):
            try:
                data = client_sock.recv()  # try to get data from socket
                if (data[0] in seq_nos):
                    if (data[0] == seq_nos[0]):  # expected sequence and hash
                        # file not on peer
                        if (data[1] == constants.SERVER_FILE_NOT_FOUND):
                            logger.error("File not found on peer")
                            self._cleanup_abnormal(data[0], hash, writer,
                                                   client_sock)
                            return False

                        # file empty
                        if (data[1] == constants.SERVER_END_PACKET):
                            connEnd = True  # connEnd is set to true
                            client_sock.send_ack(data[0])
                            break
                        # connection ends with message in buffer
                        if second_end:
                            connEnd = True
                        # server shutdown abnormal
                        if (data[1] == constants.SERVER_END_ABNORMAL):
                            self._cleanup_abnormal(data[0], hash, writer,
                                                   client_sock)
                            return False
                        hash = data[2:].decode(encoding='utf-8')

                        client_sock.send_ack(data[0])
                        self.move_window(seq_nos)  # move widow forward

                        # clear buffer and move window forward
                        if len(client_buffer) > 0:
                            self.write_buffer(client_buffer, writer)
                            self.move_window(seq_nos)
                            client_buffer.clear()
                    # buffer this, don't move the window forward
                    elif (data[0] == seq_nos[1]):
                        # connection ended by server with this packet
                        if (data[1] == constants.SERVER_END_PACKET):
                            second_end = True

                        # server shutdown abnormal
                        if (data[1] == constants.SERVER_END_ABNORMAL):
                            self._cleanup_abnormal(data[0], hash, writer,
                                                   client_sock)
                            return False

                        # append only if there isnt any data in buffer.
                        # discard packet if repeated
                        if len(client_buffer) == 0:
                            # add data to buffer
                            client_buffer.append(data[2:])
                        client_sock.send_ack(data[0])
                        retries = constants.CLIENT_MAX_RETRIES
                        raise ConnectionError  # try to get hash again
                break
            except ConnectionError:
                retries -= 1
        if (not self.serv_conn.get_conn_status()):
            logger.error("Request file failed due to server connection lost")
            self.request_cleanup(hash, writer)
            del client_sock
            return False
        if connEnd:
            logger.info("Connection complete after receiving file "
                        f"from {addr}")
            self.request_cleanup(hash, writer)
            del client_sock
            return True
        if retries < 0:
            logger.error(f"Failed P2P connection to {addr}")
            print(f"Failed P2P connection to {addr}")
            del client_sock
            self.request_cleanup(hash, writer, True)
            return False

        retries = constants.CLIENT_MAX_RETRIES
        while retries >= 0 and self.serv_conn.get_conn_status() \
                and (not connEnd):
            try:

                data = client_sock.recv()

                if (data[0] in seq_nos):
                    if (data[0] == seq_nos[0]):  # expected sequence and hash
                        # last packet by server
                        if (data[1] == constants.SERVER_END_PACKET):
                            connEnd = True
                        # connection ends with message in buffer
                        if second_end:
                            connEnd = True

                        # server shutdown abnormal
                        if (data[1] == constants.SERVER_END_ABNORMAL):
                            self._cleanup_abnormal(data[0], hash, writer,
                                                   client_sock)
                            return

                        client_sock.send_ack(data[0])
                        writer.write(data[2:])
                        self.move_window(seq_nos)

                        if len(client_buffer) > 0:
                            self.write_buffer(client_buffer, writer)
                            self.move_window(seq_nos)
                            client_buffer.clear()
                        retries = constants.CLIENT_MAX_RETRIES

                    # buffer this, don't move the window forward
                    elif (data[0] == seq_nos[1]):
                        # last packet by server
                        if (data[1] == constants.SERVER_END_PACKET):
                            second_end = True
                        # server shutdown abnormal
                        if (data[1] == constants.SERVER_END_ABNORMAL):
                            self._cleanup_abnormal(data[0], hash, writer,
                                                   client_sock)
                            return False
                        # add to buffer only if buffer empty, else possiblity
                        # of duplicates
                        if len(client_buffer) == 0:
                            client_buffer.append(data[2:])
                        client_sock.send_ack(data[0])
                        retries = constants.CLIENT_MAX_RETRIES
                        raise ConnectionError  # try to get sequence 1 again
                else:  # maybe resent data or abnormal closure
                    if (data[1] == 3):
                        self._cleanup_abnormal(data[0], hash, writer,
                                               client_sock)
                        return False
                    client_sock.send_ack(data[0])
                    retries = constants.CLIENT_MAX_RETRIES
            except ConnectionError:
                retries -= 1

        # connection to main server lost
        if (not self.serv_conn.get_conn_status()):
            self.request_cleanup(hash, writer)
            del client_sock
            return False
        if connEnd:  # connection ended
            self.request_cleanup(hash, writer)
            del client_sock
            return True
        if retries < 0:  # retries exceeded
            logger.error(f"Failed P2P connection to {addr}")
            print(f"Failed P2P connection to {addr}")
            del client_sock
            self.request_cleanup(hash, writer, True)
            return False

    def user_input(self):
        '''
        Function to get user input
        '''
        # keep looping to get user input
        while(not self.client_shutdown and self.serv_conn.get_conn_status()):
            os.system('cls' if os.name == 'nt' else 'clear')
            try:
                file_name = inputimeout(prompt="Please enter file to download"
                                        " or -1 to exit client \n>>",
                                        timeout=30.0).strip()
            except TimeoutOccurred:
                continue
            if (len(file_name) == 2):
                try:
                    if int(file_name) == -1:
                        self.serv_conn.set_close()
                        self.client_shutdown = True
                        return
                    else:
                        print("Invalid input entered")
                        time.sleep(5.0)
                except ValueError:
                    print("Invalid input entered")
                    time.sleep(5.0)
                    continue
            if file_name[-4:] == '.txt':
                try:
                    file_no = int(file_name[:-4])
                except ValueError:
                    print("Invalid input entered")
                    time.sleep(5.0)
                    continue

                if self.serv_conn.get_conn_status():
                    port, client_id = self.serv_conn.request_file(file_no)
                    if (port == -2):
                        continue
                    if (int(port) == self.my_port):
                        print("Requesting from myself...")
                        # continue
                    if (int(port) == -1):
                        print("No peers have file")
                        time.sleep(5.0)
                        continue
                    success = self.request_file(file_name[:-4],
                                                (socket.gethostbyname(
                                                    socket.gethostname()
                                                ),
                                                int(port)),
                                                self.client_file_mgr.newWrite(
                                                    self.down_loca
                                                    / (file_name[:-4] + '.txt')
                                                ))
                    if not success:
                        logger.error(f"Request for {file_name} from "
                                     f"{port} failed.")
                        print("Request failed, check logs for info.")
                        time.sleep(5.0)
                    else:
                        logger.info(f"Request for {file_name} from {client_id}"
                                    " completed successfully.")
                        print(f"Request for file {file_name} completed"
                              " successfully")
                        time.sleep(5.0)

    def __del__(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Client instance")
    parser.add_argument('client_no', metavar='C', type=int)
    args = parser.parse_args()
    # print(args.client_no)
    # input()
    client_no = args.client_no
    Path('./client_logs').mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s :: %(pathname)s:%(lineno)d ::"
                        " %(levelname)s :: %(message)s",
                        filename=f"./client_logs/log_{client_no}.log")
    client = Client(f'./configs/clients/{client_no}/{client_no}.yaml')
