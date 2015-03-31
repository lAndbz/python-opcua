
import logging
from threading import Lock
import uuid
from datetime import datetime

from opcua import ua
from opcua import utils

class PublishRequestData(object):
    def __init__(self):
        self.requesthdr = None
        self.algohdr = None
        self.seqhdr = None

class UAProcessor(object):
    def __init__(self, internal_server, socket, name):
        self.logger = logging.getLogger(__name__)
        self.iserver = internal_server
        self.name = name
        self.session = None
        self.channel = None
        self.socket = socket
        self._lock = Lock()
        self._publishdata_queue = []
        self._seq_number = 1

    def loop(self):
        #first we want a hello message
        header = ua.Header.from_stream(self.socket)
        body = self.receive_body(header.body_size)
        if header.MessageType != ua.MessageType.Hello:
            self.logger.warning("received a message which is not a hello, sending back an error message %s", header)
            hdr = ua.Header(ua.MessageType.Error, ua.ChunkType.Single)
            self.write_socket(hdr)
            return
        hello = ua.Hello.from_binary(body)
        hdr = ua.Header(ua.MessageType.Acknowledge, ua.ChunkType.Single)
        ack = ua.Acknowledge()
        ack.ReceivebufferSize = hello.ReceiveBufferSize
        ack.SendbufferSize = hello.SendBufferSize
        self.write_socket(hdr, ack)

        while True:
            header = ua.Header.from_stream(self.socket)
            if header is None:
                return
            if header.MessageType == ua.MessageType.Error:
                self.logger.warning("Received an error message type")
                return
            body = self.receive_body(header.body_size)
            if not self.process_body(header, body):
                break

    def send_response(self, requesthandle, algohdr, seqhdr, response, msgtype=ua.MessageType.SecureMessage):
        with self._lock:
            response.ResponseHeader.RequestHandle = requesthandle
            seqhdr.SequenceNumber = self._seq_number
            self._seq_number += 1
            hdr = ua.Header(msgtype, ua.ChunkType.Single, self.channel.SecurityToken.ChannelId)
            algohdr.TokenId = self.channel.SecurityToken.TokenId 
            self.write_socket(hdr, algohdr, seqhdr, response)

    def write_socket(self, hdr, *args):
        alle = []
        for arg in args:
            data = arg.to_binary()
            hdr.add_size(len(data))
            alle.append(data)
        alle.insert(0, hdr.to_binary())
        alle = b"".join(alle)
        self.logger.info("writting %s bytes to socket, with header %s ", len(alle), hdr)
        #self.logger.info("writting data %s", hdr, [i for i in args])
        #self.logger.debug("data: %s", alle)
        self.socket.send(alle)

    def receive_body(self, size):
        self.logger.debug("reading body of message (%s bytes)", size)
        data = self.socket.recv(size)
        if size != len(data):
            raise Exception("Error, did not received expected number of bytes, got {}, asked for {}".format(len(data), size))
        return utils.Buffer(data)

    def open_secure_channel(self, body):
        algohdr = ua.AsymmetricAlgorithmHeader.from_binary(body)
        seqhdr = ua.SequenceHeader.from_binary(body)
        request = ua.OpenSecureChannelRequest.from_binary(body)

        channel = self._open_secure_channel(request.Parameters)
        #send response
        response = ua.OpenSecureChannelResponse()
        response.Parameters = channel
        self.send_response(request.RequestHeader.RequestHandle, algohdr, seqhdr, response, ua.MessageType.SecureOpen)

    def forward_publish_response(self, result):
        self.logger.info("forward publish response %s", result)
        if len(self._publishdata_queue) == 0:
            self.logger.warning("Error server wants to send publish answer but no publish request is available")
            return
        response = ua.PublishResponse()
        response.Parameters = result

        requestdata = self._publishdata_queue.pop(0)
        self.send_response(requestdata.requesthdr.RequestHandle, requestdata.algohdr, requestdata.seqhdr, response)

    def process_body(self, header, body):
        if header.MessageType == ua.MessageType.SecureOpen:
            self.open_secure_channel(body)

        elif header.MessageType == ua.MessageType.SecureClose:
            if not self.channel or header.ChannelId != self.channel.SecurityToken.ChannelId:
                self.logger.warning("Request to close channel %s which was not issued, current channel is %s", header.ChannelId, self.channel)
                return False

        elif header.MessageType == ua.MessageType.SecureMessage:
            algohdr = ua.SymmetricAlgorithmHeader.from_binary(body)
            seqhdr = ua.SequenceHeader.from_binary(body)
            self.process_message(algohdr, seqhdr, body)

        else:
            self.logger.warning("Unsupported message type: %s", header.MessageType)
        return True
    
    def process_message(self, algohdr, seqhdr, body):
        typeid = ua.NodeId.from_binary(body)
        requesthdr = ua.RequestHeader.from_binary(body)
        if typeid == ua.NodeId(ua.ObjectIds.CreateSessionRequest_Encoding_DefaultBinary):
            self.logger.info("Create session request")
            params = ua.CreateSessionParameters.from_binary(body)
            
            self.session = self.iserver.create_session(self.name)#create the session on server
            sessiondata = self.session.create_session(params)#get a session creation result to send back

            response = ua.CreateSessionResponse()
            response.Parameters = sessiondata
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.CloseSessionRequest_Encoding_DefaultBinary):
            self.logger.info("Close session request")
            deletesubs = ua.unpack_uatype('Boolean', body)
            
            self.session.close_session(deletesubs)

            response = ua.CloseSessionResponse()
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.ActivateSessionRequest_Encoding_DefaultBinary):
            self.logger.info("Activate session request")
            params = ua.ActivateSessionParameters.from_binary(body) 
            
            if not self.session:
                result = ua.StatusCode(ua.StatusCodes.BadSessionIdInvalid)
            else:
                result = self.session.activate_session(params)

            response = ua.ActivateSessionResponse()
            response.Parameters = result
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.ReadRequest_Encoding_DefaultBinary):
            self.logger.info("Read request")
            params = ua.ReadParameters.from_binary(body) 
            
            results = self.session.read(params)

            response = ua.ReadResponse()
            response.Results = results
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.WriteRequest_Encoding_DefaultBinary):
            self.logger.info("Write request")
            params = ua.WriteParameters.from_binary(body) 
            
            results = self.session.write(params)

            response = ua.WriteResponse()
            response.Results = results
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.BrowseRequest_Encoding_DefaultBinary):
            self.logger.info("Browse request")
            params = ua.BrowseParameters.from_binary(body) 
            
            results = self.session.browse(params)

            response = ua.BrowseResponse()
            response.Results = results
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.GetEndpointsRequest_Encoding_DefaultBinary):
            self.logger.info("get endpoints request")
            params = ua.GetEndpointsParameters.from_binary(body) 
            
            endpoints = self.iserver.get_endpoints(params)

            response = ua.GetEndpointsResponse()
            response.Endpoints = endpoints

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.TranslateBrowsePathsToNodeIdsRequest_Encoding_DefaultBinary):
            self.logger.info("translate browsepaths to nodeids request")
            params = ua.TranslateBrowsePathsToNodeIdsParameters.from_binary(body) 
            
            paths = self.session.translate_browsepaths_to_nodeids(params.BrowsePaths)

            response = ua.TranslateBrowsePathsToNodeIdsResponse()
            response.Results = paths

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary):
            self.logger.info("add nodes request")
            params = ua.AddNodesParameters.from_binary(body) 
            
            results = self.session.add_nodes(params.NodesToAdd)

            response = ua.AddNodesResponse()
            response.Results = results

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.CreateSubscriptionRequest_Encoding_DefaultBinary):
            self.logger.info("create subscription request")
            params = ua.CreateSubscriptionParameters.from_binary(body) 
            
            result = self.session.create_subscription(params, self.forward_publish_response)

            response = ua.CreateSubscriptionResponse()
            response.Parameters = result

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.DeleteSubscriptionsRequest_Encoding_DefaultBinary):
            self.logger.info("delete subscriptions request")
            params = ua.DeleteSubscriptionsParameters.from_binary(body) 
            
            results = self.session.delete_subscriptions(params.SubscriptionIds)

            response = ua.DeleteSubscriptionsResponse()
            response.Results = results

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.CreateMonitoredItemsRequest_Encoding_DefaultBinary):
            self.logger.info("create monitored items request")
            params = ua.CreateMonitoredItemsParameters.from_binary(body) 
            
            results = self.session.create_monitored_items(params)

            response = ua.CreateMonitoredItemsResponse()
            response.Results = results

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.DeleteMonitoredItemsRequest_Encoding_DefaultBinary):
            self.logger.info("delete monitored items request")
            params = ua.DeleteMonitoredItemsParameters.from_binary(body) 
            
            results = self.session.delete_monitored_items(params)

            response = ua.DeleteMonitoredItemsResponse()
            response.Results = results

            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, response)

        elif typeid == ua.NodeId(ua.ObjectIds.PublishRequest_Encoding_DefaultBinary):
            self.logger.info("publish request")
            
            acks = ua.unpack_array("Int32", body)
            
            data = PublishRequestData()
            data.requesthdr = requesthdr
            data.seqhdr = seqhdr
            data.algohdr = algohdr
            self._publishdata_queue.append(data) # will be used to send publish answers from server
            self.session.publish(acks)




        else:
            self.logger.warning("Uknown message received %s", typeid)
            sf = ua.ServiceFault()
            sf.ResponseHeader.ServiceResult = ua.StatusCode(ua.StatusCodes.BadNotImplemented)
            self.send_response(requesthdr.RequestHandle, algohdr, seqhdr, sf)

    def _open_secure_channel(self, params):
        self.logger.info("open secure channel")
        if params.RequestType == ua.SecurityTokenRequestType.Issue:
            self.channel = ua.OpenSecureChannelResult()
            self.channel.SecurityToken.TokenId = 13 #random value
            self.channel.SecurityToken.ChannelId = self.iserver.get_new_channel_id()
            self.channel.SecurityToken.RevisedLifetime = params.RequestedLifetime 
        self.channel.SecurityToken.TokenId += 1
        self.channel.SecurityToken.CreatedAt = datetime.now()
        self.channel.SecurityToken.RevisedLifetime = params.RequestedLifetime
        self.channel.ServerNonce = uuid.uuid4().bytes + uuid.uuid4().bytes
        return self.channel


