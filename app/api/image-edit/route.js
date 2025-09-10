// app/api/image-edit/route.js
export async function POST(request) {
  try {
    // リクエストデータを拡張して受信
    const { 
      imageBase64, 
      instructions, 
      mimeType, 
      maskBase64,    // 新規追加
      editMode       // 新規追加
    } = await request.json();
    
    // バリデーション
    if (!imageBase64) {
      throw new Error('画像データが必要です');
    }
    
    if (!instructions) {
      throw new Error('編集指示が必要です');
    }
    
    // n8nへのリクエストデータを拡張
    const n8nRequestData = {
      imageBase64,
      instructions,
      mimeType: mimeType || 'image/jpeg',
      maskBase64: maskBase64 || null,        // マスク画像（オプション）
      editMode: editMode || 'full',          // 編集モード
      hasMask: !!maskBase64,                 // マスクの有無
      timestamp: new Date().toISOString(),   // タイムスタンプ
      requestId: Math.random().toString(36)  // リクエストID
    };
    
    // ログ出力（デバッグ用）
    console.log('Vercel API Request:', {
      editMode: n8nRequestData.editMode,
      hasMask: n8nRequestData.hasMask,
      imageSize: imageBase64.length,
      maskSize: maskBase64 ? maskBase64.length : 0,
      requestId: n8nRequestData.requestId
    });
    
    // n8n Cloud Webhookに送信
    const n8nResponse = await fetch('https://norion8789.app.n8n.cloud/webhook/image-edit-api-binary', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': 'Vercel-API/1.0'
      },
      body: JSON.stringify(n8nRequestData )
    });
    
    console.log('n8n Response Status:', n8nResponse.status);
    
    if (!n8nResponse.ok) {
      const errorText = await n8nResponse.text();
      throw new Error(`n8n API error: ${n8nResponse.status} - ${errorText}`);
    }
    
    // バイナリレスポンスを処理
    const imageBlob = await n8nResponse.blob();
    
    console.log('Response Image Size:', imageBlob.size);
    
    if (imageBlob.size === 0) {
      throw new Error('n8nから空の画像が返されました');
    }
    
    // 成功レスポンス
    return new Response(imageBlob, {
      status: 200,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'no-cache',
        'X-Edit-Mode': editMode || 'full',
        'X-Has-Mask': !!maskBase64 ? 'true' : 'false'
      },
    });
    
  } catch (error) {
    console.error('Vercel API Error:', error);
    
    // エラーレスポンス
    return new Response(JSON.stringify({
      error: 'Failed to process image',
      details: error.message,
      timestamp: new Date().toISOString(),
      service: 'vercel-api'
    }), {
      status: 500,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Content-Type': 'application/json',
      },
    });
  }
}

// OPTIONSリクエスト対応（CORS）
export async function OPTIONS(request) {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Accept',
      'Access-Control-Max-Age': '86400',
    },
  });
}
