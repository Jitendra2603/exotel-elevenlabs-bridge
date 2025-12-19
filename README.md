# Exotel <-> ElevenLabs Conversational AI Bridge

This project provides a bridge between Exotel's Voice API (via WebSockets) and ElevenLabs Conversational AI. It allows you to build real-time voice agents that can handle phone calls using ElevenLabs' high-quality low-latency voices and conversational logic.

## Features

- **Real-time Audio Streaming**: Bidirectional streaming between Exotel and ElevenLabs.
- **Low Latency**: Optimized chunk handling for responsive conversations.
- **Interruption Support**: Handles user interruptions by clearing the agent's audio buffer.
- **DTMF Support**: Logs DTMF digits received from Exotel.
- **Structured Logging**: Detailed logs for each call, including transcripts and technical metadata.
- **Cloud Native**: Optimized for hosting on platforms like GCP Cloud Run with console logging and production-grade serving.

## Production Readiness Improvements

The bridge has been optimized for cloud hosting with the following enhancements:
1.  **Cloud Logging**: Call-specific logs are piped to `stdout`, making them available in GCP Logs Explorer (Stackdriver).
2.  **Gunicorn + Gevent**: Uses a production-grade WSGI server with greenlet-based concurrency to handle multiple WebSocket streams efficiently.
3.  **Dynamic Configuration**: Automatically initializes settings from environment variables, removing the need for local `.env` files in production.
4.  **Ephemeral-Safe**: Fail-safes for log file creation on read-only or ephemeral filesystems.

## Local Setup

1. **Install Dependencies**:
   ```bash
   python3 -m pip install -r exotel/requirements.txt
   ```

2. **Configuration**:
   Create a `.env` file in the root directory or set the following environment variables:
   - `ELEVENLABS_AGENT_ID`: Your ElevenLabs Agent ID.
   - `ELEVENLABS_API_KEY`: Your ElevenLabs API Key.
   - `BRIDGE_PORT`: Port for the server (default: 10002).

3. **Run the Bridge**:
   ```bash
   python3 exotel/bridge.py
   ```

## GCP Deployment (Cloud Run)

To deploy the bridge to Google Cloud Run:

1. **Ensure gcloud is configured**:
   ```bash
   gcloud auth login
   gcloud config set project [YOUR_PROJECT_ID]
   ```

2. **Set required variables**:
   ```bash
   export ELEVENLABS_AGENT_ID="your_agent_id"
   export ELEVENLABS_API_KEY="your_api_key"
   ```

3. **Run the deployment script**:
   ```bash
   ./deploy_gcp.sh
   ```

The script will build the container image via Cloud Build, deploy it to Cloud Run, and provide you with the final **WebSocket URL**.

## Exotel Configuration

In your Exotel Passthru or Connect app, set the WebSocket URL to:
`wss://[YOUR_SERVICE_URL]/media`

**Recommended Settings:**
- **Audio Format**: PCM 8000Hz 16-bit Mono (standard for Exotel).
- **Inbound/Outbound**: Ensure bidirectional streaming is enabled if required.

## Architecture

The bridge uses:
- **Flask-Sock**: For handling incoming WebSocket connections from Exotel.
- **Websockets**: For the client connection to ElevenLabs.
- **Gunicorn (Gevent)**: Production server managing concurrency.
- **Multithreading**: Separate threads within each connection for audio playback and ElevenLabs session management.
