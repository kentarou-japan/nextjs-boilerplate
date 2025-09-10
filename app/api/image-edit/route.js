export const runtime = 'edge';

// CORSヘッダーを定数として定義
const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Accept, Origin',
  'Access-Control-Max-Age': '86400',
};

// OPTIONSメソッド（プリフライトリクエスト）
export async function OPTIONS(request) {
  return new Response(null, {
    status: 204,
    headers: corsHeaders,
  });
}

// GETメソッド（ヘルスチェック用）
export async function GET(request) {
  return new Response(
    JSON.stringify({ 
      status: 'ok', 
      message: 'Image editing API is running',
      timestamp: new Date().toISOString()
    }),
    {
      status: 200,
      headers: {
        ...corsHeaders,
        'Content-Type': 'application/json',
      },
    }
  );
}

// POSTメソッド（画像処理）
export async function POST(request) {
  console.log('POST request received');
  
  // すべてのレスポンスにCORSヘッダーを付与
  const responseWithCORS = (body, status, contentType = 'application/json') => {
    return new Response(body, {
      status,
      headers: {
        ...corsHeaders,
        'Content-Type': contentType,
      },
    });
  };

  try {
    // リクエストボディを取得
    const body = await request.json();
    console.log('Request body received, instructions:', body.instructions?.substring(0, 50));
    
    // 必須フィールドのチェック
    if (!body.imageBase64) {
      return responseWithCORS(
        JSON.stringify({ error: 'imageBase64 is required' }),
        400
      );
    }
    
    // n8n webhookのURLを修正（正しいワークフローIDを使用）
    const n8nWebhookUrl = 'https://norion8789.app.n8n.cloud/webhook/image-edit-api-binary';
    
    console.log('Calling n8n webhook:', n8nWebhookUrl);
    
    // n8nのWebhookを呼び出し
    const n8nResponse = await fetch(n8nWebhookUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'image/jpeg, application/json',
      },
      body: JSON.stringify({
        imageBase64: body.imageBase64,
        instructions: body.instructions || '画像の前歯二本を真っ白にホワイトニングしてください。前歯二本以外は元画像のままで変更しないでください。',
        mimeType: body.mimeType || 'image/jpeg'
      })
    });

    console.log('n8n response status:', n8nResponse.status);
    console.log('n8n response headers:', Object.fromEntries(n8nResponse.headers.entries()));

    // エラーチェック
    if (!n8nResponse.ok) {
      const errorText = await n8nResponse.text();
      console.error('n8n error response:', errorText);
      
      return responseWithCORS(
        JSON.stringify({ 
          error: 'n8n API error',
          status: n8nResponse.status,
          details: errorText.substring(0, 200),
          webhookUrl: n8nWebhookUrl
        }),
        n8nResponse.status
      );
    }

    // コンテンツタイプを確認
    const contentType = n8nResponse.headers.get('content-type');
    console.log('Response content-type:', contentType);
    
    // 画像データを取得
    const imageBlob = await n8nResponse.blob();
    console.log('Image blob size:', imageBlob.size, 'bytes');
    
    if (imageBlob.size === 0) {
      return responseWithCORS(
        JSON.stringify({ error: 'Empty response from n8n' }),
        500
      );
    }
    
    // 画像を返却（CORSヘッダー付き）
    const arrayBuffer = await imageBlob.arrayBuffer();
    return new Response(arrayBuffer, {
      status: 200,
      headers: {
        ...corsHeaders,
        'Content-Type': contentType || 'image/jpeg',
        'Content-Length': String(imageBlob.size),
        'Cache-Control': 'no-cache',
      },
    });

  } catch (error) {
    console.error('Error in image processing:', error);
    
    return responseWithCORS(
      JSON.stringify({ 
        error: 'Failed to process image',
        details: error.message,
        stack: error.stack?.substring(0, 500),
        timestamp: new Date().toISOString()
      }),
      500
    );
  }
}
