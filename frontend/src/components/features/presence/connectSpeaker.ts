import { issuePairingCode } from '../../../client/deviceAuthApi'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { pairSpeakerFromHost } from '../../../runtime/desktopBridge'
import { satellitePairCommand, type LanPairStatus } from './pairing'

export type SpeakerConnectResult = {
  code: string
  expiresAt: string
  command: string
  lanStatus: LanPairStatus
}

export async function connectRoomSpeaker(
  input: {
    nodeLabel: string
    roomName?: string
    haAreaId?: string
    nodeId?: string
    backendUrl?: string | null
  },
  onCode?: (issued: { code: string; expiresAt: string; command: string }) => void,
): Promise<SpeakerConnectResult> {
  const result = await issuePairingCode({
    node_label: input.nodeLabel,
    capabilities: ['mic', 'speaker'],
    room_name: input.roomName,
    ha_area_id: input.haAreaId,
    node_id: input.nodeId,
  })
  const command = satellitePairCommand(result.code, input.backendUrl)
  const issued = { code: result.code, expiresAt: result.expires_at, command }
  onCode?.(issued)
  if (!isDesktopApp()) {
    return { ...issued, lanStatus: 'skipped' }
  }
  try {
    const paired = await pairSpeakerFromHost({
      code: result.code,
      backendUrl: input.backendUrl,
      nodeId: input.nodeId,
    })
    return { ...issued, lanStatus: paired?.ok ? 'ok' : 'failed' }
  } catch {
    return { ...issued, lanStatus: 'failed' }
  }
}
