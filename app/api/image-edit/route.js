export const runtime = 'edge';

// すべてのレスポンスに付与するCORSヘッダー
const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Accept, Origin',
  'Access-Control-Max-Age': '86400',
};

// OPTIONSメソッド（プリフライトリクエスト）
export async function OPTIONS() {
  return new Response(null, {
    status: 204,
    headers: corsHeaders,
  });
}

// GETメソッド（ヘルスチェック）
export async function GET() {
  return new Response(
    JSON.stringify({ 
      status: 'ok', 
      message: 'Image API is running',
      cors: 'enabled'
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
  try {
    const body = await request.json();
    
    if (!body.imageBase64) {
      return new Response(
        JSON.stringify({ error: 'imageBase64 is required' }),
        {
          status: 400,
          headers: {
            ...corsHeaders,
            'Content-Type': 'application/json',
          },
        }
      );
    }
    
    // n8n webhookを呼び出し（POSTメソッドで）
    const n8nResponse = await fetch(
      'https://norion8789.app.n8n.cloud/webhook/image-edit-api-binary',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          imageBase64: body.imageBase64,
          instructions: body.instructions || '画像の前歯二本を真っ白にホワイトニングしてください。',
          mimeType: body.mimeType || 'image/jpeg'
        })
      }
    );

    // n8nからのレスポンスを確認
    if (!n8nResponse.ok) {
      const errorText = await n8nResponse.text();
      return new Response(
        JSON.stringify({ 
          error: 'n8n webhook error',
          status: n8nResponse.status,
          details: errorText
        }),
        {
          status: 502,
          headers: {
            ...corsHeaders,
            'Content-Type': 'application/json',
          },
        }
      );
    }

    // 画像データを取得してクライアントに返す
    const imageBlob = await n8nResponse.blob();
    const arrayBuffer = await imageBlob.arrayBuffer();
    
    return new Response(arrayBuffer, {
      status: 200,
      headers: {
        ...corsHeaders,  // 重要：CORSヘッダーを必ず含める
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'no-cache',
      },
    });

  } catch (error) {
    return new Response(
      JSON.stringify({ 
        error: 'Processing failed',
        message: error.message
      }),
      {
        status: 500,
        headers: {
          ...corsHeaders,
          'Content-Type': 'application/json',
        },
      }
    );
  }
}
