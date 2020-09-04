import asyncio
import datetime
import hashlib
import json
import os
import plexapi.myplex
import struct
import subprocess
import sys
import tempfile
import threading
import time

class plexConfig:

	extraLogging = True
	timeRemaining = False

	def __init__(self, serverName = "", username = "", password = "", token = "", listenForUser = "", blacklistedLibraries = None, whitelistedLibraries = None, clientID = "413407336082833418"):
		self.serverName = serverName
		self.username = username
		self.password = password
		self.token = token
		self.listenForUser = (username if listenForUser == "" else listenForUser).lower()
		self.blacklistedLibraries = blacklistedLibraries
		self.whitelistedLibraries = whitelistedLibraries
		self.clientID = clientID

plexConfigs = [
	plexConfig(serverName = "", username = "", password = "", listenForUser = "")
]

class discordRichPresence:

	def __init__(self, clientID, child):
		self.IPCPipe = ((os.environ.get("XDG_RUNTIME_DIR", None) or os.environ.get("TMPDIR", None) or os.environ.get("TMP", None) or os.environ.get("TEMP", None) or "/tmp") + "/discord-ipc-0") if isLinux else "\\\\?\\pipe\\discord-ipc-0"
		self.clientID = clientID
		self.pipeReader = None
		self.pipeWriter = None
		self.process = None
		self.running = False
		self.child = child

	async def read(self):
		try:
			data = await self.pipeReader.read(1024)
		except Exception as e:
			self.stop()

	def write(self, op, payload):
		payload = json.dumps(payload)
		self.child.log(str(payload))
		data = self.pipeWriter.write(struct.pack("<ii", op, len(payload)) + payload.encode("utf-8"))

	async def handshake(self):
		try:
			if (isLinux):
				self.pipeReader, self.pipeWriter = await asyncio.open_unix_connection(self.IPCPipe, loop = self.loop)
			else:
				self.pipeReader = asyncio.StreamReader(loop = self.loop)
				self.pipeWriter, _ = await self.loop.create_pipe_connection(lambda: asyncio.StreamReaderProtocol(self.pipeReader, loop = self.loop), self.IPCPipe)
			self.write(0, {"v": 1, "client_id": self.clientID})
			await self.read()
			self.running = True
		except Exception as e:
			self.child.log("[HANDSHAKE] " + str(e))

	def start(self):
		self.child.log("Connecting to Discord")
		emptyProcessFilePath = tempfile.gettempdir() + ("/" if isLinux else "\\") + "discordRichPresencePlex-emptyProcess.py"
		if (not os.path.exists(emptyProcessFilePath)):
			with open(emptyProcessFilePath, "w") as emptyProcessFile:
				emptyProcessFile.write("import time\n\ntry:\n\twhile (True):\n\t\ttime.sleep(3600)\nexcept:\n\tpass")
		self.process = subprocess.Popen(["python3" if isLinux else "pythonw", emptyProcessFilePath])
		self.loop = asyncio.new_event_loop() if isLinux else asyncio.ProactorEventLoop()
		self.loop.run_until_complete(self.handshake())

	def stop(self):
		self.child.log("Disconnecting from Discord")
		self.child.lastState, self.child.lastSessionKey, self.child.lastRatingKey = None, None, None
		self.process.kill()
		if (self.child.stopTimer):
			self.child.stopTimer.cancel()
			self.child.stopTimer = None
		if (self.child.stopTimer2):
			self.child.stopTimer2.cancel()
			self.child.stopTimer2 = None
		if (self.pipeWriter):
			try:
				self.pipeWriter.close()
			except:
				pass
			self.pipeWriter = None
		if (self.pipeReader):
			try:
				self.loop.run_until_complete(self.pipeReader.read(1024))
			except:
				pass
			self.pipeReader = None
		try:
			self.loop.close()
		except:
			pass
		self.running = False

	def send(self, activity):
		payload = {
			"cmd": "SET_ACTIVITY",
			"args": {
				"activity": activity,
				"pid": self.process.pid
			},
			"nonce": "{0:.20f}".format(time.time())
		}
		self.write(1, payload)
		self.loop.run_until_complete(self.read())

class discordRichPresencePlex(discordRichPresence):

	productName = "Plex Media Server"
	stopTimerInterval = 5
	stopTimer2Interval = 35
	checkConnectionTimerInterval = 60
	maximumIgnores = 3

	def __init__(self, plexConfig):
		self.plexConfig = plexConfig
		self.instanceID = hashlib.md5(str(id(self)).encode("UTF-8")).hexdigest()[:5]
		super().__init__(plexConfig.clientID, self)
		self.plexAccount = None
		self.plexServer = None
		self.isServerOwner = False
		self.plexAlertListener = None
		self.lastState = None
		self.lastSessionKey = None
		self.lastRatingKey = None
		self.stopTimer = None
		self.stopTimer2 = None
		self.checkConnectionTimer = None
		self.ignoreCount = 0

	def run(self):
		self.reset()
		connected = False
		while (not connected):
			try:
				if (self.plexConfig.token):
					self.plexAccount = plexapi.myplex.MyPlexAccount(self.plexConfig.username, token = self.plexConfig.token)
				else:
					self.plexAccount = plexapi.myplex.MyPlexAccount(self.plexConfig.username, self.plexConfig.password)
				self.log("Logged in as Plex User \"" + self.plexAccount.username + "\"")
				self.plexServer = None
				for resource in self.plexAccount.resources():
					if (resource.product == self.productName and resource.name == self.plexConfig.serverName):
						self.plexServer = resource.connect()
						try:
							self.plexServer.account()
							self.isServerOwner = True
						except:
							pass
						self.log("Connected to " + self.productName + " \"" + self.plexConfig.serverName + "\"")
						self.plexAlertListener = self.plexServer.startAlertListener(self.onPlexServerAlert)
						#self.log("Listening for PlaySessionStateNotification alerts from user \"" + self.plexConfig.listenForUser + "\"")
						if (self.checkConnectionTimer):
							self.checkConnectionTimer.cancel()
							self.checkConnectionTimer = None
						self.checkConnectionTimer = threading.Timer(self.checkConnectionTimerInterval, self.checkConnection)
						self.checkConnectionTimer.start()
						connected = True
						break
				if (not self.plexServer):
					self.log(self.productName + " \"" + self.plexConfig.serverName + "\" not found")
					break
			except Exception as e:
				self.log("Failed to connect to Plex: " + str(e))
				self.log("Reconnecting in 10 seconds")
				time.sleep(10)

	def reset(self):
		if (self.running):
			self.stop()
		self.plexAccount, self.plexServer = None, None
		if (self.plexAlertListener):
			try:
				self.plexAlertListener.stop()
			except:
				pass
			self.plexAlertListener = None
		if (self.stopTimer):
			self.stopTimer.cancel()
			self.stopTimer = None
		if (self.stopTimer2):
			self.stopTimer2.cancel()
			self.stopTimer2 = None
		if (self.checkConnectionTimer):
			self.checkConnectionTimer.cancel()
			self.checkConnectionTimer = None

	def checkConnection(self):
		try:
			self.log("Request for clients list to check connection: " + str(self.plexServer.clients()), extra = True)
			self.checkConnectionTimer = threading.Timer(self.checkConnectionTimerInterval, self.checkConnection)
			self.checkConnectionTimer.start()
		except Exception as e:
			self.log("Connection to Plex lost: " + str(e))
			self.log("Reconnecting")
			self.run()

	def log(self, text, colour = "", extra = False):
		timestamp = datetime.datetime.now().strftime("%I:%M:%S %p")
		prefix = "[" + timestamp + "] [" + self.plexConfig.serverName + "/" + self.instanceID + "] "
		lock.acquire()
		if (extra):
			if (self.plexConfig.extraLogging):
				print(prefix + colourText(str(text), colour))
		else:
			print(prefix + colourText(str(text), colour))
		lock.release()

	def onPlexServerAlert(self, data):
		if (not self.plexServer):
			return
		try:
			if (data["type"] == "playing" and "PlaySessionStateNotification" in data):
				sessionData = data["PlaySessionStateNotification"][0]
				state = sessionData["state"]
				sessionKey = int(sessionData["sessionKey"])
				ratingKey = int(sessionData["ratingKey"])
				viewOffset = int(sessionData["viewOffset"])
				metadata = self.plexServer.fetchItem(ratingKey)
				libraryName = metadata.section().title
				if (isinstance(self.plexConfig.blacklistedLibraries, list)):
					if (libraryName in self.plexConfig.blacklistedLibraries):
						return
				if (isinstance(self.plexConfig.whitelistedLibraries, list)):
					if (libraryName not in self.plexConfig.whitelistedLibraries):
						return
				if (self.lastSessionKey == sessionKey and self.lastRatingKey == ratingKey):
					if (self.stopTimer2):
						self.stopTimer2.cancel()
						self.stopTimer2 = None
					if (self.lastState == state):
						if (self.ignoreCount == self.maximumIgnores):
							self.ignoreCount = 0
						else:
							self.ignoreCount += 1
							self.stopTimer2 = threading.Timer(self.stopTimer2Interval, self.stopOnNoUpdate)
							self.stopTimer2.start()
							return
					elif (state == "stopped"):
						self.lastState, self.lastSessionKey, self.lastRatingKey = None, None, None
						self.stopTimer = threading.Timer(self.stopTimerInterval, self.stop)
						self.stopTimer.start()
						return
				elif (state == "stopped"):
					return
				if (self.isServerOwner):
					plexServerSessions = self.plexServer.sessions()
					if (len(plexServerSessions) < 1):
						return
					for session in plexServerSessions:
						sessionFound = False
						if (session.sessionKey == sessionKey):
							sessionFound = True
							if (session.usernames[0].lower() == self.plexConfig.listenForUser):
								break
							else:
								return
					if (not sessionFound):
						#self.log("No matching session found", "red", True)
						return
				if (self.stopTimer):
					self.stopTimer.cancel()
					self.stopTimer = None
				if (self.stopTimer2):
					self.stopTimer2.cancel()
				self.stopTimer2 = threading.Timer(self.stopTimer2Interval, self.stopOnNoUpdate)
				self.stopTimer2.start()
				self.lastState, self.lastSessionKey, self.lastRatingKey = state, sessionKey, ratingKey
				mediaType = metadata.type
				if (state != "playing"):
					extra = secondsToText(viewOffset / 1000, ":") + "/" + secondsToText(metadata.duration / 1000, ":")
				else:
					extra = secondsToText(metadata.duration / 1000)
				if (mediaType == "movie"):
					title = metadata.title + " (" + str(metadata.year) + ")"
					extra = extra + " · " + ", ".join([genre.tag for genre in metadata.genres[:3]])
					largeText = "Watching a Movie"
				elif (mediaType == "episode"):
					title = metadata.grandparentTitle
					extra = extra + " · S" + str(metadata.parentIndex) + " · E" + str(metadata.index) + " - " + metadata.title
					largeText = "Watching a TV Show"
				elif (mediaType == "track"):
					title = metadata.title
					artist = metadata.originalTitle
					if (not artist):
						artist = metadata.grandparentTitle
					extra = artist + " · " + metadata.parentTitle
					largeText = "Listening to Music"
				else:
					self.log("Unsupported media type \"" + mediaType + "\", ignoring", "red", True)
					return
				activity = {
					"details": title,
					"state": extra,
					"assets": {
						"large_text": largeText,
						"large_image": "logo",
						"small_text": state.capitalize(),
						"small_image": state
					},
				}
				if (state == "playing"):
					currentTimestamp = int(time.time())
					if (self.plexConfig.timeRemaining):
						activity["timestamps"] = {"end": round(currentTimestamp + ((metadata.duration - viewOffset) / 1000))}
					else:
						activity["timestamps"] = {"start": round(currentTimestamp - (viewOffset / 1000))}
				if (not self.running):
					self.start()
				if (self.running):
					self.send(activity)
				else:
					self.stop()
		except Exception as e:
			self.log("onPlexServerAlert Error: " + str(e))

	def stopOnNoUpdate(self):
		self.log("No updates from session key " + str(self.lastSessionKey) + ", stopping", "red", True)
		self.stop()

isLinux = sys.platform in ["linux", "darwin"]
lock = threading.Semaphore(value = 1)

os.system("clear" if isLinux else "cls")

if (len(plexConfigs) == 0):
	print("Error: plexConfigs list is empty")
	sys.exit()

colours = {
	"red": "91",
	"green": "92",
	"yellow": "93",
	"blue": "94",
	"magenta": "96",
	"cyan": "97"
}

def colourText(text, colour = ""):
	prefix = ""
	suffix = ""
	colour = colour.lower()
	if (colour in colours):
		prefix = "\033[" + colours[colour] + "m"
		suffix = "\033[0m"
	return prefix + str(text) + suffix

def secondsToText(seconds, joiner = ""):
	seconds = round(seconds)
	text = {"h": seconds // 3600, "m": seconds // 60 % 60, "s": seconds % 60}
	if (joiner == ""):
		text = [str(v) + k for k, v in text.items() if v > 0]
	else:
		if (text["h"] == 0):
			del text["h"]
		text = [str(v).rjust(2, "0") for k, v in text.items()]
	return joiner.join(text)

discordRichPresencePlexInstances = []
for config in plexConfigs:
	discordRichPresencePlexInstances.append(discordRichPresencePlex(config))
try:
	for discordRichPresencePlexInstance in discordRichPresencePlexInstances:
		discordRichPresencePlexInstance.run()
	while (True):
		time.sleep(3600)
except KeyboardInterrupt:
	for discordRichPresencePlexInstance in discordRichPresencePlexInstances:
		discordRichPresencePlexInstance.reset()
except Exception as e:
	print("Error: " + str(e))